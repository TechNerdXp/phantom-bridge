"""Daily energy: what the inverter counted, and what we integrated.

The inverter keeps its own per-day counters -- `QED<yyyymmdd>` for PV watt-
hours and `QLD<yyyymmdd>` for load watt-hours -- and on this unit they answer
(confirmed 2026-09-20, with month and year forms alongside). They are the
authoritative "today" numbers and, because they go back before this project
existed, the history the panel-cleaning trend is judged against.

What the inverter does not count is the battery and the grid, so those are
integrated here from the poll samples: watts x seconds, with a cap on the
gap so a stalled link does not integrate a stale reading across an hour.

Everything lands in logs/energy.json, one record per day.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import statistics

# A gap longer than this between samples is a hole in the data, not a flat
# line: nothing is integrated across it.
MAX_GAP_S = 30.0


def blank_day() -> dict:
    return {"pv_wh": None, "load_wh": None,          # inverter counters
            "batt_out_wh": 0.0, "batt_in_wh": 0.0,   # integrated here
            "grid_wh": 0.0, "load_int_wh": 0.0, "pv_int_wh": 0.0,
            "on_battery_s": 0.0, "outage_s": 0.0,
            "peak_load_w": 0.0, "peak_load_at": None,
            "peak_pv_w": 0.0, "peak_pv_at": None,
            "soc_low": None, "soc_low_at": None,      # the pack's day, for the
            "soc_high": None, "soc_high_at": None,    # backup question
            "samples": 0}


class DayBook:
    """Accumulates one day's energy from flow samples; persists to a file."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.days: dict = {}
        self.load()
        self._last_t: float | None = None

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.days = payload.get("days") or {}
        except (OSError, ValueError):
            self.days = {}

    def save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"days": self.days}, indent=1, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(self.path)

    def day(self, date: dt.date) -> dict:
        key = date.isoformat()
        if key not in self.days:
            self.days[key] = blank_day()
        return self.days[key]

    # -- feeding it --------------------------------------------------------

    def add_sample(self, when: dt.datetime, t: float, flow: dict) -> None:
        """Integrate one poll sample. `t` is a monotonic-ish seconds clock."""
        rec = self.day(when.date())
        rec["samples"] += 1
        load = float(flow.get("load_w") or 0.0)
        pv = float(flow.get("pv_w") or 0.0)
        if load > rec["peak_load_w"]:
            rec["peak_load_w"], rec["peak_load_at"] = load, when.strftime("%H:%M")
        if pv > rec["peak_pv_w"]:
            rec["peak_pv_w"], rec["peak_pv_at"] = pv, when.strftime("%H:%M")
        soc = flow.get("batt_soc")
        if soc is not None:
            # Older day records predate these keys; setdefault keeps them safe.
            if rec.setdefault("soc_low", None) is None or soc < rec["soc_low"]:
                rec["soc_low"], rec["soc_low_at"] = soc, when.strftime("%H:%M")
            if rec.setdefault("soc_high", None) is None or soc > rec["soc_high"]:
                rec["soc_high"], rec["soc_high_at"] = soc, when.strftime("%H:%M")

        if self._last_t is not None:
            gap = t - self._last_t
            if 0 < gap <= MAX_GAP_S:
                h = gap / 3600.0
                rec["batt_out_wh"] += float(flow.get("batt_discharge_w") or 0.0) * h
                rec["batt_in_wh"] += float(flow.get("batt_charge_w") or 0.0) * h
                rec["grid_wh"] += float(flow.get("grid_w") or 0.0) * h
                rec["load_int_wh"] += load * h
                rec["pv_int_wh"] += pv * h
                if flow.get("on_battery"):
                    rec["on_battery_s"] += gap
                if not flow.get("grid_present", True):
                    rec["outage_s"] += gap
        self._last_t = t

    def set_counters(self, date: dt.date, pv_wh=None, load_wh=None) -> None:
        rec = self.day(date)
        if pv_wh is not None:
            rec["pv_wh"] = pv_wh
        if load_wh is not None:
            rec["load_wh"] = load_wh

    # -- reading it --------------------------------------------------------

    def pv_baseline(self, before: dt.date, days: int = 7) -> float | None:
        """Median PV Wh over the `days` complete days before `before`.

        >>> book = DayBook(pathlib.Path("nonexistent.json"))
        >>> for d, wh in [(1, 12000), (2, 11000), (3, 13000), (4, 0)]:
        ...     book.set_counters(dt.date(2026, 9, d), pv_wh=wh)
        >>> book.pv_baseline(dt.date(2026, 9, 5), 7)   # the 0 day is skipped
        12000
        """
        values = []
        for back in range(1, days + 1):
            rec = self.days.get((before - dt.timedelta(days=back)).isoformat())
            if rec and rec.get("pv_wh"):
                values.append(rec["pv_wh"])
        return statistics.median(values) if values else None

    def summary(self, date: dt.date) -> dict:
        """What the watch screen and tray show for one day."""
        rec = self.days.get(date.isoformat()) or blank_day()
        yesterday = self.days.get((date - dt.timedelta(days=1)).isoformat()) or {}
        out = dict(rec)
        out["date"] = date.isoformat()
        out["yesterday_pv_wh"] = yesterday.get("pv_wh")
        out["yesterday_load_wh"] = yesterday.get("load_wh")
        # Judged on yesterday's full day, not today's partial one: a half-day
        # total is not a drop, it is lunchtime.
        out["pv_baseline_wh"] = self.pv_baseline(date - dt.timedelta(days=1))
        return out


def pv_verdict(yesterday_wh, baseline_wh, warn_fraction: float = 0.7) -> str | None:
    """A one-line reading of yesterday against the trailing baseline.

    >>> pv_verdict(8000, 12000)
    'yesterday 8.0 kWh, 33% below the 7-day typical 12.0 -- check the panels'
    >>> pv_verdict(12500, 12000)
    'yesterday 12.5 kWh, 7-day typical 12.0'
    >>> pv_verdict(None, 12000) is None
    True
    """
    if not yesterday_wh or not baseline_wh:
        return None
    ratio = yesterday_wh / baseline_wh
    if ratio < warn_fraction:
        return (f"yesterday {yesterday_wh / 1000:.1f} kWh, {(1 - ratio) * 100:.0f}% "
                f"below the 7-day typical {baseline_wh / 1000:.1f} -- check the panels")
    return (f"yesterday {yesterday_wh / 1000:.1f} kWh, 7-day typical "
            f"{baseline_wh / 1000:.1f}")


if __name__ == "__main__":
    import doctest
    print(doctest.testmod())
