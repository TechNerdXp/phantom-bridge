"""Every day's analysis, since inception, read from the logs and cached.

Shared by the history window, insights.py and the collector's output-
priority autopilot, which is why it lives here and not in history.py: the
collector must not import a GDI window to learn what yesterday looked like.

A finished day is parsed once from its JSONL and cached as
logs/days/<date>.json, keyed on the size of the log it came from. Today is
never cached: it is read incrementally by DayLog.

Once a finished day is older than yesterday and its cache is current, its
log is archived as <date>.jsonl.gz (archive_old_logs, run by the collector
once a day). A day's log is ~40 MB of JSON and compresses 15:1, so a year is
about a gigabyte instead of fifteen. An archived day is final, so its cache
is trusted as it stands. Today and yesterday stay plain, for grep.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import pathlib
import shutil
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
import pi30

# Today's log is re-read (incrementally) no more often than this.
REFRESH_S = 60.0

SLIM_KEYS = ("load_w", "pv_w", "grid_w", "batt_w", "batt_v", "batt_soc",
             "batt_charge_a", "batt_discharge_a", "batt_charge_w",
             "batt_discharge_w", "on_battery", "grid_present")

DAYS_DIR = bridge.LOG_DIR / "days"

# Bumped when what a day's analysis counts changes, so every cached day is
# rebuilt once. 2: a record whose reply is the wrong shape for its command
# (pi30.plausible -- a late answer to the previous command) is not a sample.
CACHE_VERSION = 2

# Plain JSONL is kept for this many days, today included; older finished
# days are archived.
KEEP_PLAIN_DAYS = 2

# Only the QMOD and QPIGS reads are ever parsed, a third of a day's lines. A
# byte test goes first, so the rest never reach json.loads.
_READ = b'"event": "read"'
_WANTED = (b'"cmd": "QPIGS"', b'"cmd": "QMOD"')


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
        self.samples: list[dict] = []
        self._offset = 0
        self._final = False              # read whole from the archive
        self._mode = None
        self.loaded_at = 0.0
        self.analysis: dict | None = None

    @property
    def path(self) -> pathlib.Path | None:
        return bridge.log_path(self.day)

    def refresh(self, force: bool = False) -> bool:
        """Read whatever has been appended. True if there is new data."""
        if not force and time.time() - self.loaded_at < REFRESH_S:
            return False
        self.loaded_at = time.time()
        path = self.path
        if path is None:
            return False
        added = 0
        if path.suffix == ".gz":
            # Archived: final, read whole once. A log that was being followed
            # as plain JSONL starts again from the archive, which holds all
            # of it.
            if self._final:
                return False
            self._final, self._offset, self._mode, self.samples = True, 0, None, []
            try:
                with bridge.open_log(path) as handle:
                    for raw in handle:
                        added += self._take(raw)
            except (OSError, EOFError):
                return False
        else:
            try:
                size = path.stat().st_size
            except OSError:
                return False
            if size < self._offset:                     # truncated
                self._offset, self._mode, self.samples = 0, None, []
            if size == self._offset:
                return False
            with path.open("rb") as handle:
                handle.seek(self._offset)
                for raw in handle:
                    if not raw.endswith(b"\n"):
                        break                           # a partial line waits
                    self._offset += len(raw)
                    added += self._take(raw)
        if added:
            self.analysis = None
        return bool(added)

    def _take(self, raw: bytes) -> int:
        """One log line: 1 if it added a sample, else 0."""
        if _READ not in raw or not any(w in raw for w in _WANTED):
            return 0
        try:
            rec = json.loads(raw)
        except ValueError:
            return 0
        cmd = rec.get("cmd")
        if (rec.get("event") != "read" or not rec.get("ok")
                or not pi30.plausible(cmd or "", rec.get("text") or "")):
            return 0
        payload = rec.get("data") or {}
        if cmd == "QMOD":
            self._mode = payload.get("mode")
            return 0
        if cmd != "QPIGS" or not payload:
            return 0
        try:
            when = dt.datetime.fromisoformat(rec["local"])
        except (KeyError, ValueError):
            return 0
        f = flowmod.analyse(payload, mode=self._mode, trust_soc=config.TRUST_SOC,
                            grid_present_volts=config.GRID_PRESENT_VOLTS)
        self.samples.append({"t": when, "flow": {k: f.get(k) for k in SLIM_KEYS}})
        return 1

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
        path = bridge.log_path(day)
        try:
            size = path.stat().st_size if path is not None else 0
        except OSError:
            size = 0
        if not size:
            rec = insights.analyse_day(day, [], counters)
            self._mem[key] = rec
            return rec
        final = path.suffix == ".gz"
        cache = DAYS_DIR / f"{key}.json"
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            # An archived day is final and was cached before it was archived,
            # from the plain log; the archive's own size does not match that.
            if payload.get("v") == CACHE_VERSION and (final or payload.get("log_size") == size):
                rec = payload["analysis"]
                rec["pv_wh"], rec["load_wh"] = counters.get("pv_wh"), counters.get("load_wh")
                self._mem[key] = rec
                return rec
        except (OSError, ValueError, KeyError):
            pass
        # A day already open in the history window is not parsed twice.
        log = self._logs.get(day) or DayLog(day)
        log.refresh(force=True)
        rec = log.analysed(counters)
        try:
            DAYS_DIR.mkdir(exist_ok=True)
            cache.write_text(json.dumps({"v": CACHE_VERSION, "log_size": size,
                                         "analysis": rec}), encoding="utf-8")
        except OSError:
            pass
        self._mem[key] = rec
        return rec

    def first_day(self) -> dt.date:
        """The earliest day anything is known about."""
        days = [k for k, v in self.book().items()
                if v.get("pv_wh") or v.get("load_wh") or v.get("samples")]
        days += [p.name[:10] for pattern in ("????-??-??.jsonl", "????-??-??.jsonl.gz")
                 for p in bridge.LOG_DIR.glob(pattern)]
        try:
            return min(dt.date.fromisoformat(d) for d in days)
        except ValueError:
            return bridge.now_site().date()


# --------------------------------------------------------------------------
# archiving
# --------------------------------------------------------------------------

def _same(plain: pathlib.Path, packed: pathlib.Path) -> bool:
    """Does the archive read back as the plain file, byte for byte?"""
    step = 1 << 20
    with plain.open("rb") as a, gzip.open(packed, "rb") as b:
        while True:
            x, y = a.read(step), b.read(step)
            if x != y:
                return False
            if not x:
                return True


def archive_old_logs(store: DayStore, today: dt.date, log=None) -> list[dict]:
    """Compress the plain log of every finished day older than yesterday.

    A day is archived only once its cache is current, so nothing has to read
    it again: <date>.jsonl.gz.tmp is written, read back and compared with
    the plain file byte for byte, renamed into place, and only then is the
    plain file deleted. Anything that fails leaves the plain file where it
    was, to be tried again the next day. Returns what was archived.
    """
    cutoff = today - dt.timedelta(days=KEEP_PLAIN_DAYS - 1)
    done = []
    for plain in sorted(bridge.LOG_DIR.glob("????-??-??.jsonl")):
        try:
            day = dt.date.fromisoformat(plain.stem)
        except ValueError:
            continue
        if day >= cutoff:
            continue
        packed = plain.with_name(plain.name + ".gz")
        tmp = plain.with_name(plain.name + ".gz.tmp")
        try:
            size = plain.stat().st_size
            store.day(day)                     # the cache, current, first
            with plain.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as out:
                shutil.copyfileobj(src, out, 1 << 20)
            if not _same(plain, tmp):
                raise OSError("the archive does not read back as the log")
            tmp.replace(packed)
            plain.unlink()
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            if log:
                log({"event": "log-archive-failed", "day": day.isoformat(),
                     "error": str(exc)})
            continue
        done.append({"day": day.isoformat(), "bytes": size,
                     "archived": packed.stat().st_size})
    return done
