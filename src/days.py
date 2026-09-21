"""Every day's analysis, since inception, read from the logs and cached.

Shared by the history window and the collector's output-priority autopilot,
which is why it lives here and not in history.py: the collector must not
import a GDI window to learn what yesterday looked like.

A finished day is parsed once from its JSONL and cached as
logs/days/<date>.json, keyed on the size of the log it came from. Today is
never cached: it is read incrementally by DayLog.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _path in (str(_ROOT), str(_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import bridge
import config
import flow as flowmod
import insights

# Today's log is re-read (incrementally) no more often than this.
REFRESH_S = 60.0

SLIM_KEYS = ("load_w", "pv_w", "grid_w", "batt_w", "batt_v", "batt_soc",
             "batt_charge_a", "batt_discharge_a", "batt_charge_w",
             "batt_discharge_w", "on_battery", "grid_present")

DAYS_DIR = bridge.LOG_DIR / "days"


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

class DayLog:
    """One day's samples, read incrementally from its JSONL file.

    Keeps a slim copy of each analysed sample (the dozen keys the charts and
    insights.analyse_day need, not the whole flow dict) so a full day at the
    one-second on-battery cadence stays in the tens of megabytes, not the
    hundreds.
    """

    def __init__(self, day: dt.date):
        self.day = day
        self.path = bridge.LOG_DIR / f"{day.isoformat()}.jsonl"
        self.samples: list[dict] = []
        self._offset = 0
        self._tail = b""
        self._mode = None
        self.loaded_at = 0.0
        self.analysis: dict | None = None

    def refresh(self, force: bool = False) -> bool:
        """Read whatever has been appended. True if there is new data."""
        if not force and time.time() - self.loaded_at < REFRESH_S:
            return False
        self.loaded_at = time.time()
        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        if size < self._offset:                     # rotated or truncated
            self._offset, self._tail, self.samples = 0, b"", []
        if size == self._offset:
            return False
        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
            self._offset = handle.tell()
        data = self._tail + chunk
        lines = data.split(b"\n")
        self._tail = lines.pop()                    # a partial last line waits
        added = 0
        for raw in lines:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("event") != "read" or not rec.get("ok"):
                continue
            cmd, payload = rec.get("cmd"), rec.get("data") or {}
            if cmd == "QMOD":
                self._mode = payload.get("mode")
            elif cmd == "QPIGS" and payload:
                try:
                    when = dt.datetime.fromisoformat(rec["local"])
                except (KeyError, ValueError):
                    continue
                f = flowmod.analyse(payload, mode=self._mode, trust_soc=config.TRUST_SOC,
                                    grid_present_volts=config.GRID_PRESENT_VOLTS)
                self.samples.append({"t": when, "flow": {k: f.get(k) for k in SLIM_KEYS}})
                added += 1
        if added:
            self.analysis = None
        return bool(added)

    def analysed(self, counters: dict | None) -> dict:
        if self.analysis is None:
            self.analysis = insights.analyse_day(self.day, self.samples, counters)
        return self.analysis


class DayStore:
    """Every day's analysis, since inception.

    A finished day is parsed once and cached as logs/days/<date>.json with
    the size of the log it came from, so the cache is rebuilt only if the
    log changes. Today is never cached: it comes from the live DayLog.
    """

    def __init__(self):
        self._mem: dict[str, dict] = {}
        self._logs: dict[dt.date, DayLog] = {}
        self._book: dict = {}
        self._book_at = 0.0

    def book(self) -> dict:
        if time.time() - self._book_at > REFRESH_S:
            self._book = insights.load_energy_book()
            self._book_at = time.time()
        return self._book

    def live(self, day: dt.date) -> DayLog:
        """The incremental log for a day being looked at (today, usually)."""
        log = self._logs.get(day)
        if log is None:
            # At most two live logs: today and the day on screen.
            for old in [d for d in self._logs if d != bridge.now_site().date()]:
                del self._logs[old]
            log = self._logs[day] = DayLog(day)
            log.refresh(force=True)
        else:
            log.refresh()
        return log

    def drop(self) -> None:
        self._logs.clear()
        self._mem.clear()

    def day(self, day: dt.date) -> dict:
        key = day.isoformat()
        counters = self.book().get(key) or {}
        today = bridge.now_site().date()
        if day >= today:
            return self.live(day).analysed(counters) if day == today else {
                "date": key, "samples": 0}
        if key in self._mem:
            return self._mem[key]
        path = bridge.LOG_DIR / f"{key}.jsonl"
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        cache = DAYS_DIR / f"{key}.json"
        if size:
            try:
                payload = json.loads(cache.read_text(encoding="utf-8"))
                if payload.get("log_size") == size:
                    rec = payload["analysis"]
                    rec["pv_wh"], rec["load_wh"] = counters.get("pv_wh"), counters.get("load_wh")
                    self._mem[key] = rec
                    return rec
            except (OSError, ValueError, KeyError):
                pass
            log = DayLog(day)
            log.refresh(force=True)
            rec = log.analysed(counters)
            try:
                DAYS_DIR.mkdir(exist_ok=True)
                cache.write_text(json.dumps({"log_size": size, "analysis": rec}),
                                 encoding="utf-8")
            except OSError:
                pass
        else:
            rec = insights.analyse_day(day, [], counters)
        self._mem[key] = rec
        return rec

    def first_day(self) -> dt.date:
        """The earliest day anything is known about."""
        days = [k for k, v in self.book().items()
                if v.get("pv_wh") or v.get("load_wh") or v.get("samples")]
        days += [p.stem for p in bridge.LOG_DIR.glob("????-??-??.jsonl")]
        try:
            return min(dt.date.fromisoformat(d) for d in days)
        except ValueError:
            return bridge.now_site().date()
