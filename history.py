#!/usr/bin/env python3
"""The power history: day, week, month and year in review, drawn.

Opened from the tray ("Power history"), from the watch screen (click the
day strip at the bottom), or on its own:

    python history.py                     today, day tab
    python history.py --date 2026-09-19   one day
    python history.py --tab month         start on a tab: day|week|month|year
    python history.py --png out.png       render one frame to a file and exit

Four tabs, one window:

  DAY    Midnight to midnight as curves: solar as a filled area, the house
         as a line, the derived grid leg as a line, the state of charge on
         its own 0-100 % axis on the right. Under it the battery's strip,
         charging up from the centre in green, discharging down in amber;
         every on-battery episode shaded amber across both, every grid
         outage red. Then the day's facts, its on-battery episodes, and
         its hour-by-hour profile.

  WEEK   The calendar week: a pair of bars per day (solar and house kWh,
  MONTH  the battery's share as an amber inset, the trailing 7-day typical
         solar as a line), the typical hour of that range (average house
         and solar by hour, how often the pack was carrying it), and the
         review: totals, medians, best and worst days, when usage peaks,
         when the sun carries the house, when the pack is fullest and
         lowest, how often and why it ran on battery, and the pack
         capacity the episodes imply.

  YEAR   The same review over the calendar year, with one bar per month.

The history is complete since inception, not a window of days: every past
day's analysis is computed once from its log and cached under logs/days/,
so a year in review is a read of a few hundred small files, not a parse of
gigabytes. Days before logging began still appear with the inverter's own
daily counters. Today is live: its log is read incrementally -- only the
lines appended since the last look -- once a minute while the window is
open, and dropped when it closes.

Keys: Left / Right move by the tab's unit, Home returns to today, D W M Y
switch tabs. Click a day bar to open that day; click a month bar to open
that month. Same window plumbing as the watch screen (it subclasses
WatchWindow: GDI+ with a GDI fallback, DPI aware, double buffered).

Reads logs/<date>.jsonl, logs/energy.json and logs/days/. Never the link.
"""
from __future__ import annotations

import calendar
import ctypes
import datetime as dt
import json
import pathlib
import statistics
import sys
import time
from ctypes import wintypes

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

import bridge
import config
import flow as flowmod
import insights
import watch
from days import DAYS_DIR, REFRESH_S, SLIM_KEYS, DayLog, DayStore  # noqa: F401
from watch import (BAD, BATT, BG, GRID, INK, INK_DIM, INK_FAINT, LINE, LOAD,
                   PANEL, SOLAR, WARN, DT_CENTER, DT_LEFT, DT_RIGHT, Canvas,
                   user32)

WM_KEYDOWN, WM_LBUTTONDOWN = 0x0100, 0x0201
VK_LEFT, VK_RIGHT, VK_HOME = 0x25, 0x27, 0x24

TABS = ("day", "week", "month", "year")

def _best(rec: dict, counter: str, integ: str) -> float:
    """The inverter's counter, or what we integrated if the counter lags."""
    return max(rec.get(counter) or 0, rec.get(integ) or 0)


def _minutes(hm: str | None) -> int | None:
    if not hm:
        return None
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _hm(minutes: float | None) -> str:
    if minutes is None:
        return "--"
    return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"


def _median_time(values: list[str | None]) -> str:
    mins = [m for m in (_minutes(v) for v in values) if m is not None]
    return _hm(statistics.median(mins)) if mins else "--"


def review(days: list[dict]) -> dict:
    """Everything the week / month / year tabs show, from per-day analyses."""
    with_data = [d for d in days if d.get("samples")]
    out = {"days": len(days), "logged": len(with_data),
           "pv_wh": sum(_best(d, "pv_wh", "pv_int_wh") for d in days),
           "load_wh": sum(_best(d, "load_wh", "load_int_wh") for d in days),
           "batt_out_wh": sum(d.get("batt_out_wh") or 0 for d in with_data),
           "batt_in_wh": sum(d.get("batt_in_wh") or 0 for d in with_data),
           "grid_wh": sum(d.get("grid_wh") or 0 for d in with_data),
           "on_battery_s": sum(d.get("on_battery_s") or 0 for d in with_data),
           "outage_s": sum(d.get("outage_s") or 0 for d in with_data)}

    pv_days = [(d["date"], _best(d, "pv_wh", "pv_int_wh")) for d in days
               if _best(d, "pv_wh", "pv_int_wh") > 0]
    if pv_days:
        vals = [v for _, v in pv_days]
        out["pv_median"] = statistics.median(vals)
        out["pv_best"] = max(pv_days, key=lambda p: p[1])
        out["pv_worst"] = min(pv_days, key=lambda p: p[1])
    load_days = [(d["date"], _best(d, "load_wh", "load_int_wh")) for d in days
                 if _best(d, "load_wh", "load_int_wh") > 0]
    if load_days:
        out["load_median"] = statistics.median(v for _, v in load_days)
        out["load_top"] = max(load_days, key=lambda p: p[1])
    peaks = [(d.get("peak_load_w") or 0, d["date"], d.get("peak_load_at")) for d in with_data]
    if peaks:
        out["peak_load"] = max(peaks)

    # the typical hour
    load_h = [[] for _ in range(24)]
    pv_h = [[] for _ in range(24)]
    batt_h = [[] for _ in range(24)]
    grid_h = [[] for _ in range(24)]
    for d in with_data:
        for h in d.get("hours") or []:
            if h["n"]:
                load_h[h["hour"]].append(h["load_w"])
                pv_h[h["hour"]].append(h["pv_w"])
                batt_h[h["hour"]].append(h["on_battery"] or 0)
                grid_h[h["hour"]].append(h["grid_absent"] or 0)
    prof = [{"hour": i,
             "load_w": statistics.fmean(load_h[i]) if load_h[i] else None,
             "pv_w": statistics.fmean(pv_h[i]) if pv_h[i] else None,
             "on_battery": statistics.fmean(batt_h[i]) if batt_h[i] else None,
             "grid_absent": statistics.fmean(grid_h[i]) if grid_h[i] else None,
             "n": len(load_h[i])} for i in range(24)]
    out["hours"] = prof
    have = [i for i in range(24) if prof[i]["load_w"] is not None]
    out["peak_hours"] = sorted(sorted(have, key=lambda i: -prof[i]["load_w"])[:3])
    sunny = [i for i in have if (prof[i]["pv_w"] or 0) >= insights.PV_LIVE_W]
    out["sun_hours"] = (sunny[0], sunny[-1] + 1) if sunny else None
    out["best_sun_hour"] = max(sunny, key=lambda i: prof[i]["pv_w"]) if sunny else None
    out["batt_hours"] = [i for i in have if (prof[i]["on_battery"] or 0) >= 0.5]

    out["sun_first"] = _median_time([d.get("pv_first") for d in with_data])
    out["sun_last"] = _median_time([d.get("pv_last") for d in with_data])
    out["full_at"] = _median_time([d.get("soc_max_at") for d in with_data])
    out["low_at"] = _median_time([d.get("soc_min_at") for d in with_data])
    socs = [d["soc_min"] for d in with_data if d.get("soc_min") is not None]
    out["soc_low_median"] = statistics.median(socs) if socs else None

    eps = [dict(e, date=d["date"]) for d in with_data for e in d.get("episodes") or []]
    out["episodes"] = eps
    closed = [e for e in eps if not e.get("open") and not e.get("truncated")]
    out["episodes_priority"] = sum(1 for e in closed if e["kind"] == "by priority")
    out["episodes_outage"] = sum(1 for e in closed if e["kind"] == "outage")
    implied = [e["implied_pack_wh"] for e in closed if e.get("implied_pack_wh")]
    out["implied"] = (statistics.median(implied), min(implied), max(implied), len(implied)) \
        if implied else None
    return out


def _nice_ceiling(value: float, steps=(100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000)) -> float:
    """The smallest 'round' number at or above value, for an axis top."""
    value = max(value, 1.0)
    for step in steps:
        for n in range(1, 11):
            if step * n >= value:
                return float(step * n)
    return float(steps[-1] * 10)


def _columns(samples: list[dict], width: int) -> list[dict | None]:
    """Fold the day's samples into one bucket per pixel column.

    Peaks are what a chart is for, so watts keep the column maximum; SOC
    keeps the last reading; on-battery and outage are 'any'. A column with
    no sample is None, and the curves break there rather than bridging it.
    """
    cols: list[dict | None] = [None] * width
    for sample in samples:
        t = sample["t"]
        sec = t.hour * 3600 + t.minute * 60 + t.second
        i = min(width - 1, int(sec * width / 86400))
        f = sample["flow"]
        c = cols[i]
        if c is None:
            c = cols[i] = {"pv": 0.0, "load": 0.0, "grid": 0.0, "chg": 0.0, "dis": 0.0,
                           "soc": None, "on_batt": False, "outage": False}
        c["pv"] = max(c["pv"], f.get("pv_w") or 0.0)
        c["load"] = max(c["load"], f.get("load_w") or 0.0)
        c["grid"] = max(c["grid"], f.get("grid_w") or 0.0)
        c["chg"] = max(c["chg"], f.get("batt_charge_w") or 0.0)
        c["dis"] = max(c["dis"], f.get("batt_discharge_w") or 0.0)
        if f.get("batt_soc") is not None:
            c["soc"] = f["batt_soc"]
        c["on_batt"] = c["on_batt"] or bool(f.get("on_battery"))
        c["outage"] = c["outage"] or not f.get("grid_present", True)
    return cols


def _runs(cols, key):
    """Contiguous column ranges where cols[i][key] is true: [(start, end)]."""
    out, start = [], None
    for i, c in enumerate(cols):
        on = bool(c and c[key])
        if on and start is None:
            start = i
        elif not on and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(cols)))
    return out


def _segments(cols, key, x0, y_of):
    """Polyline segments for one series, broken at empty columns."""
    segs, cur = [], []
    for i, c in enumerate(cols):
        if c is None or c.get(key) is None:
            if len(cur) > 1:
                segs.append(cur)
            cur = []
            continue
        cur.append((x0 + i + 0.5, y_of(c[key])))
    if len(cur) > 1:
        segs.append(cur)
    return segs


def _hours(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return "0 min"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _kwh(wh) -> str:
    return "--" if wh is None else f"{wh / 1000:.1f} kWh"


def _hour_runs(hours: list[int]) -> str:
    if not hours:
        return "--"
    runs, start, prev = [], hours[0], hours[0]
    for h in hours[1:]:
        if h != prev + 1:
            runs.append((start, prev))
            start = h
        prev = h
    runs.append((start, prev))
    return ", ".join(f"{a:02d}-{b + 1:02d}" for a, b in runs)


def _week_of(day: dt.date) -> tuple[dt.date, dt.date]:
    start = day - dt.timedelta(days=day.weekday())
    return start, start + dt.timedelta(days=6)


def _month_of(day: dt.date) -> tuple[dt.date, dt.date]:
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1), day.replace(day=last)


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------

class HistoryWindow(watch.WatchWindow):
    """Day, week, month, year. One per tray process, like the watch screen."""

    CLASS = "PhantomBridgeHistory"
    TITLE = "Phantom II - power history"
    W, H = 1080, 860

    def __init__(self, force_gdi: bool = False):
        super().__init__(force_gdi)
        self.day = bridge.now_site().date()
        self.tab = "day"
        self.store = DayStore()
        self._targets: list[tuple[float, float, float, float, object]] = []

    def close(self):
        super().close()
        self.store.drop()                   # a closed window holds no day

    # -- navigation -------------------------------------------------------------

    def show_day(self, day: dt.date, tab: str | None = None) -> None:
        today = bridge.now_site().date()
        self.day = min(day, today)
        if tab:
            self.tab = tab
        self.refresh()

    def _step(self, n: int) -> None:
        d = self.day
        if self.tab == "day":
            d += dt.timedelta(days=n)
        elif self.tab == "week":
            d += dt.timedelta(days=7 * n)
        elif self.tab == "month":
            month = d.month - 1 + n
            year = d.year + month // 12
            month = month % 12 + 1
            d = d.replace(year=year, month=month,
                          day=min(d.day, calendar.monthrange(year, month)[1]))
        else:
            year = d.year + n
            d = d.replace(year=year, day=min(d.day, calendar.monthrange(year, d.month)[1]))
        self.show_day(d)

    def _on_input(self, msg, wparam, lparam) -> bool:
        if msg == WM_KEYDOWN:
            key = {VK_LEFT: ("step", -1), VK_RIGHT: ("step", 1), VK_HOME: ("home", None),
                   ord("D"): ("tab", "day"), ord("W"): ("tab", "week"),
                   ord("M"): ("tab", "month"), ord("Y"): ("tab", "year")}.get(wparam)
            if not key:
                return False
            kind, arg = key
            if kind == "step":
                self._step(arg)
            elif kind == "home":
                self.show_day(bridge.now_site().date())
            else:
                self.show_day(self.day, arg)
            return True
        if msg == WM_LBUTTONDOWN:
            x = (lparam & 0xFFFF) / self.s
            y = ((lparam >> 16) & 0xFFFF) / self.s
            for x0, y0, x1, y1, action in self._targets:
                if x0 <= x <= x1 and y0 <= y <= y1:
                    kind, arg = action
                    if kind == "step":
                        self._step(arg)
                    elif kind == "home":
                        self.show_day(bridge.now_site().date())
                    elif kind == "tab":
                        self.show_day(self.day, arg)
                    elif kind == "open":
                        self.show_day(arg[1], arg[0])
                    return True
            return True
        return False

    def _target(self, x, y, w, h, action) -> None:
        self._targets.append((x, y, x + w, y + h, action))

    # -- painting -------------------------------------------------------------------

    def _paint(self, hdc, dev_w, dev_h):
        width, height = dev_w / self.s, dev_h / self.s
        self._fill(hdc, 0, 0, width, height, BG)
        self._targets = []
        pad = 20
        today = bridge.now_site().date()

        # -- header and tabs
        self._text(hdc, "Power history", pad, 12, 300, 26, INK, 17, 600)
        self._text(hdc, "since " + self.store.first_day().strftime("%d %b %Y")
                   + "  -  keys: Left / Right, Home, D W M Y",
                   pad, 36, 420, 16, INK_FAINT, 11)
        tx = pad
        for name in TABS:
            w = 62
            on = name == self.tab
            if on:
                self._fill(hdc, tx, 78, w, 2, INK)
            self._text(hdc, name.upper(), tx, 58, w, 20, INK if on else INK_DIM, 12, 700, DT_CENTER)
            self._target(tx, 56, w, 26, ("tab", name))
            tx += w + 6

        if self.tab == "day":
            title = self.day.strftime("%a %d %b %Y") + ("  (today)" if self.day == today else "")
            first, last = self.day, self.day
        elif self.tab == "week":
            first, last = _week_of(self.day)
            title = f"week {first.isocalendar()[1]}  -  {first:%d %b} to {last:%d %b %Y}"
        elif self.tab == "month":
            first, last = _month_of(self.day)
            title = first.strftime("%B %Y")
        else:
            first, last = self.day.replace(month=1, day=1), self.day.replace(month=12, day=31)
            title = str(self.day.year)
        nav_prev = (width - pad - 340, 14, 26, 24)
        nav_next = (width - pad - 26, 14, 26, 24)
        nav_home = (width - pad - 310, 14, 280, 24)
        self._text(hdc, "<", *nav_prev, INK_DIM, 14, 700, DT_CENTER)
        self._text(hdc, ">", *nav_next, INK_DIM if last < today else INK_FAINT, 14, 700, DT_CENTER)
        self._text(hdc, title, *nav_home, INK, 15, 700, DT_CENTER)
        self._target(*nav_prev, ("step", -1))
        self._target(*nav_next, ("step", 1))
        self._target(*nav_home, ("home", None))

        if self.tab == "day":
            self._paint_day(hdc, width, height, pad)
        else:
            # Nothing is known before inception, so a year or month that
            # started earlier is reviewed from there -- "2 of 263 days" would
            # count days before the project existed.
            inception = self.store.first_day()
            if self.tab != "year":
                first = max(first, inception)
            self._paint_review(hdc, width, height, pad, first, max(first, min(last, today)),
                               inception)

    # -- the day tab --------------------------------------------------------------

    def _paint_day(self, hdc, width, height, pad):
        day = self.store.day(self.day)
        samples = self.store.live(self.day).samples if self.day == bridge.now_site().date() \
            else None
        if samples is None:
            # A past day: its samples are drawn from the log once, then dropped
            # with the DayLog -- the analysis itself stays cached.
            samples = self.store.live(self.day).samples

        cx0, cw = pad + 44, width - pad * 2 - 44 - 44
        main_y, main_h = 112, 200
        batt_y, batt_h = main_y + main_h + 12, 56
        facts_y = batt_y + batt_h + 44
        prof_y = facts_y + 196
        prof_h = height - prof_y - 40

        ncols = int(cw)
        cols = _columns(samples, ncols)
        live = [c for c in cols if c]
        wmax = _nice_ceiling(max([c["pv"] for c in live] + [c["load"] for c in live]
                                 + [c["grid"] for c in live] + [500.0]))
        bmax = _nice_ceiling(max([c["chg"] for c in live] + [c["dis"] for c in live] + [100.0]))

        def y_w(w):
            return main_y + main_h - main_h * min(w, wmax) / wmax

        def y_soc(soc):
            return main_y + main_h - main_h * max(0, min(100, soc)) / 100.0

        bmid = batt_y + batt_h / 2

        cv = Canvas(hdc, self.s, self.force_gdi)
        try:
            cv.rect(cx0, main_y, cw, main_h, PANEL, alpha=120)
            cv.rect(cx0, batt_y, cw, batt_h, PANEL, alpha=120)
            for hour in range(0, 25, 3):
                x = cx0 + cw * hour / 24
                cv.lines([(x, main_y), (x, batt_y + batt_h)], LINE, 1.0, alpha=160)
            for frac in (0.25, 0.5, 0.75):
                y = main_y + main_h * frac
                cv.lines([(cx0, y), (cx0 + cw, y)], LINE, 1.0, alpha=110)
            cv.lines([(cx0, bmid), (cx0 + cw, bmid)], LINE, 1.0)
            for start, end in _runs(cols, "on_batt"):
                cv.rect(cx0 + start, main_y, max(1, end - start), batt_y + batt_h - main_y,
                        WARN, alpha=34)
            for start, end in _runs(cols, "outage"):
                cv.rect(cx0 + start, main_y, max(1, end - start), batt_y + batt_h - main_y,
                        BAD, alpha=60)
            for seg in _segments(cols, "pv", cx0, y_w):
                base = main_y + main_h
                cv.polygon(seg + [(seg[-1][0], base), (seg[0][0], base)], SOLAR, alpha=70)
                cv.lines(seg, SOLAR, 1.4)
            for seg in _segments(cols, "grid", cx0, y_w):
                cv.lines(seg, GRID, 1.4)
            for seg in _segments(cols, "load", cx0, y_w):
                cv.lines(seg, LOAD, 1.6)
            for seg in _segments(cols, "soc", cx0, y_soc):
                cv.lines(seg, INK, 1.2, alpha=170)
            for i, c in enumerate(cols):
                if c is None:
                    continue
                x = cx0 + i
                if c["chg"] > 0:
                    h = (batt_h / 2 - 2) * min(c["chg"], bmax) / bmax
                    cv.rect(x, bmid - h, 1, h, BATT, alpha=220)
                if c["dis"] > 0:
                    h = (batt_h / 2 - 2) * min(c["dis"], bmax) / bmax
                    cv.rect(x, bmid, 1, h, WARN, alpha=220)
            cv.rect(pad, facts_y - 14, width - pad * 2, 1, LINE)
            self._profile_shapes(cv, cx0, prof_y, cw, prof_h, day.get("hours") or [])
        finally:
            cv.close()

        # axes and legend
        for hour in range(0, 25, 3):
            x = cx0 + cw * hour / 24
            self._mono(hdc, f"{hour:02d}", x - 14, batt_y + batt_h + 2, 28, 14, INK_FAINT, 11)
        for frac in (0.0, 0.5, 1.0):
            w = wmax * frac
            tick = (f"{w / 1000:.1f}kW" if wmax >= 1000 else f"{w:.0f}W") if w else "0"
            self._mono(hdc, tick, pad - 6, y_w(w) - 7, 46, 14, INK_FAINT, 11, DT_RIGHT)
            self._mono(hdc, f"{100 * frac:.0f}%", cx0 + cw + 4, y_soc(100 * frac) - 7,
                       40, 14, INK_FAINT, 11, DT_LEFT)
        self._mono(hdc, f"+{bmax:.0f}", pad - 6, batt_y - 2, 46, 14, BATT, 11, DT_RIGHT)
        self._mono(hdc, f"-{bmax:.0f}", pad - 6, batt_y + batt_h - 12, 46, 14, WARN, 11, DT_RIGHT)
        self._mono(hdc, "W", pad - 6, bmid - 7, 46, 14, INK_FAINT, 11, DT_RIGHT)
        self._legend(hdc, cx0, main_y - 18, [("solar", SOLAR), ("house", LOAD),
                                             ("grid (derived)", GRID), ("SOC", INK),
                                             ("on battery", WARN), ("grid out", BAD)])
        if not samples:
            self._text(hdc, "No samples logged for this day.", cx0, main_y + main_h / 2 - 10,
                       cw, 20, INK_DIM, 12, 400, DT_CENTER)

        # facts and episodes
        col_w = (width - pad * 2) / 2
        fx, fy = pad, facts_y
        pv_day = _best(day, "pv_wh", "pv_int_wh")
        load_day = _best(day, "load_wh", "load_int_wh")
        facts = [
            ("solar", f"{_kwh(pv_day)}  peak {day.get('peak_pv_w') or 0:,.0f} W {day.get('peak_pv_at') or '--'}"
                      f"  sun {day.get('pv_first') or '--'}-{day.get('pv_last') or '--'}", SOLAR),
            ("house", f"{_kwh(load_day)}  peak {day.get('peak_load_w') or 0:,.0f} W "
                      f"{day.get('peak_load_at') or '--'}  grid ~{_kwh(day.get('grid_wh') or 0)}", LOAD),
            ("battery", f"out {_kwh(day.get('batt_out_wh') or 0)}  in {_kwh(day.get('batt_in_wh') or 0)}"
                        f"  charging {day.get('charge_first') or '--'}-{day.get('charge_last') or '--'}",
             BATT),
            ("SOC", (f"lowest {day['soc_min']}% at {day['soc_min_at']}  fullest "
                     f"{day['soc_max']}% at {day['soc_max_at']}")
                    if day.get("soc_min") is not None else "no readings", INK_DIM),
            ("time", f"on battery {_hours(day.get('on_battery_s'))}  grid out "
                     f"{_hours(day.get('outage_s'))}  logged {day.get('coverage_h') or 0:.1f} h",
             INK_DIM),
        ]
        for i, (name, text, colour) in enumerate(facts):
            y = fy + i * 21
            self._text(hdc, name.upper(), fx, y, 64, 18, colour, 11, 700)
            self._mono(hdc, text, fx + 66, y, col_w - 74, 18, INK_DIM, 12, DT_LEFT)
        self._episodes(hdc, pad + col_w, fy, col_w - 10, day.get("episodes") or [], rows=5)

        self._text(hdc, "HOUR BY HOUR", cx0, prof_y - 24, 200, 18, INK_FAINT, 12, 700)
        self._profile_text(hdc, cx0, prof_y, cw, prof_h, pad, day.get("hours") or [])

    # -- week / month / year ------------------------------------------------------

    def _paint_review(self, hdc, width, height, pad, first: dt.date, last: dt.date,
                      inception: dt.date):
        today = bridge.now_site().date()
        n_days = (last - first).days + 1
        dates = [first + dt.timedelta(days=i) for i in range(n_days)]
        recs = [self.store.day(d) for d in dates]
        # The review counts from inception; the bars keep the calendar.
        rv = review([r for d, r in zip(dates, recs) if d >= inception])

        cx0, cw = pad + 44, width - pad * 2 - 44 - 10
        bars_y, bars_h = 112, 190
        prof_y, prof_h = bars_y + bars_h + 58, 130
        facts_y = prof_y + prof_h + 44

        # what the bars are: days, or months for the year
        if self.tab == "year":
            groups = []
            for month in range(1, 13):
                m_first = first.replace(month=month, day=1)
                if m_first > today:
                    break
                m_recs = [r for d, r in zip(dates, recs) if d.month == month]
                groups.append((m_first.strftime("%b"), ("month", m_first),
                               sum(_best(r, "pv_wh", "pv_int_wh") for r in m_recs),
                               sum(_best(r, "load_wh", "load_int_wh") for r in m_recs),
                               sum(r.get("batt_out_wh") or 0 for r in m_recs),
                               m_first.month == self.day.month))
            typical = []
        else:
            groups = [(d.strftime("%d") if self.tab == "month" else d.strftime("%a %d"),
                       ("day", d), _best(r, "pv_wh", "pv_int_wh"),
                       _best(r, "load_wh", "load_int_wh"), r.get("batt_out_wh") or 0,
                       d == self.day) for d, r in zip(dates, recs)]
            # trailing 7-day typical solar, which reaches before the range
            typical = []
            for d in dates:
                window = []
                for back in range(1, 8):
                    prev = self.store.book().get((d - dt.timedelta(days=back)).isoformat()) or {}
                    v = _best(prev, "pv_wh", "pv_int_wh")
                    if v > 0:
                        window.append(v)
                typical.append(statistics.median(window) if len(window) >= 3 else None)

        kmax = _nice_ceiling(max([g[2] for g in groups] + [g[3] for g in groups] + [1000.0]) / 1000.0,
                             steps=(1, 2, 5, 10, 20, 50, 100, 200, 500, 1000))
        slot = cw / max(1, len(groups))

        cv = Canvas(hdc, self.s, self.force_gdi)
        try:
            cv.rect(cx0, bars_y, cw, bars_h, PANEL, alpha=120)
            for frac in (0.25, 0.5, 0.75):
                y = bars_y + bars_h * frac
                cv.lines([(cx0, y), (cx0 + cw, y)], LINE, 1.0, alpha=110)
            bw = max(2.0, slot * 0.36)
            for i, (label, target, pv, load, out, selected) in enumerate(groups):
                x = cx0 + slot * i
                if selected:
                    cv.rect(x, bars_y, slot, bars_h, INK, alpha=22)
                self._target(x, bars_y, slot, bars_h, ("open", target))
                for j, (wh, colour) in enumerate(((pv, SOLAR), (load, LOAD))):
                    if wh <= 0:
                        continue
                    h = bars_h * min(wh, kmax * 1000) / (kmax * 1000)
                    cv.rect(x + slot * 0.1 + j * bw, bars_y + bars_h - h, bw - 1, h, colour, alpha=210)
                    if j == 1 and out > 0:
                        hh = bars_h * min(out, kmax * 1000) / (kmax * 1000)
                        cv.rect(x + slot * 0.1 + j * bw, bars_y + bars_h - hh, bw - 1, hh, WARN, alpha=230)
            pts = [(cx0 + slot * i + slot / 2,
                    bars_y + bars_h - bars_h * min(v, kmax * 1000) / (kmax * 1000))
                   for i, v in enumerate(typical) if v]
            if len(pts) > 1:
                cv.lines(pts, INK_DIM, 1.2, alpha=200)
            self._profile_shapes(cv, cx0, prof_y, cw, prof_h, rv["hours"])
            cv.rect(pad, facts_y - 14, width - pad * 2, 1, LINE)
        finally:
            cv.close()

        unit = "MONTH" if self.tab == "year" else "DAY"
        self._text(hdc, f"BY {unit}", cx0, bars_y - 24, 120, 18, INK_FAINT, 12, 700)
        legend = [("solar kWh", SOLAR), ("house kWh", LOAD), ("of which from battery", WARN)]
        if typical:
            legend.append(("7-day typical solar", INK_DIM))
        self._legend(hdc, cx0 + 80, bars_y - 22, legend)
        for frac in (0.0, 0.5, 1.0):
            self._mono(hdc, f"{kmax * frac:.0f}" + (" kWh" if frac == 1.0 else ""),
                       pad - 6, bars_y + bars_h - bars_h * frac - 7, 46, 14, INK_FAINT, 11, DT_RIGHT)
        step = 1 if slot >= 26 else 2 if slot >= 14 else 5
        for i, (label, *_rest) in enumerate(groups):
            if i % step == 0 or i == len(groups) - 1:
                self._mono(hdc, label, cx0 + slot * i, bars_y + bars_h + 2, slot, 14, INK_FAINT, 11)

        self._text(hdc, "THE TYPICAL HOUR", cx0, prof_y - 24, 200, 18, INK_FAINT, 12, 700)
        self._mono(hdc, f"averaged over the {rv['logged']} logged day{'s' if rv['logged'] != 1 else ''}",
                   cx0 + 140, prof_y - 22, 300, 16, INK_FAINT, 11, DT_LEFT)
        self._profile_text(hdc, cx0, prof_y, cw, prof_h, pad, rv["hours"])

        # the review: a table on the left, the regularities on the right
        fx, fy = pad, facts_y
        label_w, cell_w, rh = 70, 110, 21
        self._text(hdc, "TOTALS", fx, fy, 200, 18, INK_FAINT, 12, 700)
        heads = [("solar", SOLAR), ("house", LOAD), ("battery", BATT), ("grid", GRID)]
        for j, (name, colour) in enumerate(heads):
            self._text(hdc, name.upper(), fx + label_w + cell_w * j, fy, cell_w, 18, colour, 11, 700)

        def dm(date_wh):          # "17.2 (09-07)"
            return f"{date_wh[1] / 1000:.1f} ({date_wh[0][5:]})"

        peak = rv.get("peak_load")
        table = [
            ("total", [_kwh(rv["pv_wh"]), _kwh(rv["load_wh"]),
                       f"out {rv['batt_out_wh'] / 1000:.1f}", f"~{_kwh(rv['grid_wh'])}"]),
            ("per day", [f"med {rv['pv_median'] / 1000:.1f}" if rv.get("pv_median") is not None else "--",
                         f"med {rv['load_median'] / 1000:.1f}" if rv.get("load_median") is not None else "--",
                         f"in {rv['batt_in_wh'] / 1000:.1f}", "derived"]),
            ("best", [dm(rv["pv_best"]) if rv.get("pv_best") else "--",
                      f"{peak[0]:,.0f} W" if peak else "--",
                      f"on {_hours(rv['on_battery_s'])}", f"out {_hours(rv['outage_s'])}"]),
            ("worst", [dm(rv["pv_worst"]) if rv.get("pv_worst") else "--",
                       f"({peak[1][5:]} {peak[2]})" if peak else "",
                       f"{rv['episodes_priority'] + rv['episodes_outage']} episodes",
                       f"{rv['episodes_outage']} outage{'s' if rv['episodes_outage'] != 1 else ''}"]),
        ]
        for i, (name, cells) in enumerate(table):
            y = fy + rh * (i + 1)
            self._text(hdc, name.upper(), fx, y, label_w, 18, INK_FAINT, 12, 700)
            for j, cell in enumerate(cells):
                self._mono(hdc, cell, fx + label_w + cell_w * j, y, cell_w, 18, INK_DIM, 12, DT_LEFT)
        self._mono(hdc, f"{rv['logged']} of {rv['days']} days have samples; the rest show "
                        "the inverter's counters", fx, fy + rh * 5 + 4, label_w + cell_w * 4, 18,
                   INK_FAINT, 11, DT_LEFT)

        rx = fx + label_w + cell_w * 4 + 24
        rw = width - pad - rx
        self._text(hdc, "REGULARITIES", rx, fy, 200, 18, INK_FAINT, 12, 700)
        reg = []
        prof = rv["hours"]
        if rv["peak_hours"]:
            reg.append(("peaks " + ", ".join(
                f"{i:02d}:00 {prof[i]['load_w']:,.0f} W" for i in rv["peak_hours"]), LOAD))
        if rv["sun_hours"]:
            a, b = rv["sun_hours"]
            best = rv["best_sun_hour"]
            reg.append((f"solar carries {a:02d}-{b:02d}h, best {best:02d}:00 "
                        f"({prof[best]['pv_w']:,.0f} W)", SOLAR))
        reg.append((f"sun on the array typically {rv['sun_first']}-{rv['sun_last']}", SOLAR))
        low = f"{rv['soc_low_median']:.0f}%" if rv.get("soc_low_median") is not None else "--"
        reg.append((f"pack fullest ~{rv['full_at']}, lowest ({low}) ~{rv['low_at']}", BATT))
        reg.append(("on battery mostly " + _hour_runs(rv["batt_hours"]) + "h"
                    if rv["batt_hours"] else "rarely on battery for a whole hour", WARN))
        reg.append((f"{rv['episodes_priority']} episodes by priority (grid present), "
                    f"{rv['episodes_outage']} in outages", WARN))
        if rv["implied"]:
            med, lo, hi, n = rv["implied"]
            reg.append((f"implied pack {med / 1000:.1f} kWh per 100% SOC, {n} episodes "
                        f"({lo / 1000:.1f}-{hi / 1000:.1f})", INK_DIM))
            if config.BATTERY_CAPACITY_AH:
                rated = config.BATTERY_CAPACITY_AH * 51.2
                reg.append((f"rated {rated / 1000:.1f} kWh: implied is {med / rated * 100:.0f}% of it",
                            INK_DIM))
        else:
            reg.append(("implied pack: no closed episode with a 5%+ SOC drop yet", INK_FAINT))
        reg.append(("SOC is the inverter's voltage estimate; Wh are measured", INK_FAINT))
        for i, (text, colour) in enumerate(reg[:9]):
            y = fy + rh * (i + 1)
            self._fill(hdc, rx, y + 5, 4, 9, colour)
            self._mono(hdc, text, rx + 10, y, rw - 10, 18, INK_DIM, 12, DT_LEFT)

    # -- shared pieces ----------------------------------------------------------------

    def _legend(self, hdc, x, y, items) -> None:
        for name, colour in items:
            self._fill(hdc, x, y + 6, 10, 3, colour)
            self._mono(hdc, name, x + 14, y, 180, 15, INK_DIM, 11, DT_LEFT)
            x += 14 + 8.4 * len(name) + 18

    def _profile_shapes(self, cv, x0, y0, w, h, hours) -> None:
        """24 slots: house average as bars, solar as a line, the share of the
        hour spent on the pack as an amber floor, without grid as red."""
        cv.rect(x0, y0, w, h, PANEL, alpha=120)
        if not hours:
            return
        slot = w / 24
        wmax = _nice_ceiling(max([hh.get("load_w") or 0 for hh in hours]
                                 + [hh.get("pv_w") or 0 for hh in hours] + [200.0]))
        self._prof_wmax = wmax
        for hh in hours:
            i = hh["hour"]
            if not hh.get("n"):
                continue
            x = x0 + slot * i
            share = hh.get("on_battery") or 0
            if share > 0:
                cv.rect(x, y0 + h - h * share, slot, h * share, WARN, alpha=45)
            absent = hh.get("grid_absent") or 0
            if absent > 0:
                cv.rect(x, y0 + h - h * absent, slot, h * absent, BAD, alpha=60)
            load = hh.get("load_w") or 0
            bh = h * min(load, wmax) / wmax
            cv.rect(x + slot * 0.2, y0 + h - bh, slot * 0.6, bh, LOAD, alpha=200)
        seg = []
        for hh in sorted(hours, key=lambda x: x["hour"]):
            if hh.get("n") and hh.get("pv_w") is not None:
                seg.append((x0 + slot * hh["hour"] + slot / 2,
                            y0 + h - h * min(hh["pv_w"], wmax) / wmax))
            else:
                if len(seg) > 1:
                    cv.lines(seg, SOLAR, 1.6)
                seg = []
        if len(seg) > 1:
            cv.lines(seg, SOLAR, 1.6)

    def _profile_text(self, hdc, x0, y0, w, h, pad, hours) -> None:
        slot = w / 24
        for i in range(0, 24, 3):
            self._mono(hdc, f"{i:02d}", x0 + slot * i, y0 + h + 2, slot, 14, INK_FAINT, 11)
        wmax = getattr(self, "_prof_wmax", None)
        if wmax:
            for frac in (0.0, 1.0):
                v = wmax * frac
                tick = (f"{v / 1000:.1f}kW" if wmax >= 1000 else f"{v:.0f}W") if v else "0"
                self._mono(hdc, tick, pad - 6, y0 + h - h * frac - 7, 46, 14, INK_FAINT, 11, DT_RIGHT)
        self._legend(hdc, x0 + w - 500, y0 - 22, [("house avg", LOAD), ("solar avg", SOLAR),
                                                  ("share on battery", WARN), ("grid out", BAD)])
        if not hours or not any(hh.get("n") for hh in hours):
            self._text(hdc, "nothing logged", x0, y0 + h / 2 - 10, w, 20, INK_FAINT, 11, 400, DT_CENTER)

    def _episodes(self, hdc, x, y, w, eps, rows=6) -> None:
        self._text(hdc, "ON-BATTERY EPISODES", x, y, 300, 18, INK_FAINT, 12, 700)
        self._mono(hdc, "length   drawn   avg   SOC   pack",
                   x + 230, y, w - 230, 18, INK_FAINT, 11, DT_LEFT)
        if not eps:
            self._mono(hdc, "none", x, y + 21, 200, 18, INK_FAINT, 12, DT_LEFT)
        for i, ep in enumerate(eps[:rows]):
            yy = y + 21 * (i + 1)
            soc = (f"{ep['soc_start']}>{ep['soc_end']}%"
                   if ep.get("soc_start") is not None and ep.get("soc_end") is not None else "--")
            pack = f"~{ep['implied_pack_wh'] / 1000:.1f} kWh" if ep.get("implied_pack_wh") else ""
            tail = " open" if ep.get("open") else " cut" if ep.get("truncated") else ""
            line = (f"{ep['start']}-{ep['end']} {_hours(ep['duration_s']):>7} {ep['wh']:>6,} Wh "
                    f"{ep['avg_w']:>5,} W  {soc:<7} {pack}{tail}")
            colour = BAD if ep["kind"] == "outage" else WARN
            self._fill(hdc, x, yy + 5, 4, 9, colour)
            self._mono(hdc, line, x + 10, yy, w - 20, 18, INK_DIM, 12, DT_LEFT)
        if len(eps) > rows:
            self._mono(hdc, f"+{len(eps) - rows} more  (insights.py --date {self.day} lists all)",
                       x + 10, y + 21 * (rows + 1), w - 20, 18, INK_FAINT, 11, DT_LEFT)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="the power history, on its own")
    parser.add_argument("--date", help="YYYY-MM-DD (default today)")
    parser.add_argument("--tab", choices=TABS, default="day")
    parser.add_argument("--png", metavar="PATH", help="render one frame to a PNG and exit")
    parser.add_argument("--gdi", action="store_true", help="force the plain-GDI fallback")
    parser.add_argument("--scale", type=float, help="display scale for --png (default: system)")
    args = parser.parse_args()
    watch.set_dpi_aware()
    win = HistoryWindow(force_gdi=args.gdi)
    win.tab = args.tab
    if args.date:
        win.day = dt.date.fromisoformat(args.date)
    if args.png:
        win.render_png(args.png, args.scale)
        print(f"wrote {args.png}")
        raise SystemExit(0)
    win.open()
    msg = wintypes.MSG()
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
