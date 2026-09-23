#!/usr/bin/env python3
"""Usage insights from the logs: when the day peaks, when the sun carries
the house, when the battery fills and when it empties.

    python insights.py                    the last 7 days
    python insights.py --days 30
    python insights.py --date 2026-09-19  one day in full
    python insights.py --out logs/insights.txt
    python insights.py --json out.json    the numbers, for anything downstream

Everything here is read from the logs and logs/energy.json, through the same
day cache the history window and the autopilot use (src/days.py): a
finished day is parsed once, ever. No link is opened, so it can run while
the collector is polling and never competes for the dongle.

What it is for, in the words of the plan: recorded facts to plan the next
setup and to adjust the priority settings (SBU / USB) against, and the
evidence for the battery-backup question -- how the SOC drops under load and
at rest, per episode, so a claim rests on numbers rather than a feeling.

Two caveats it prints rather than hides:

  The SOC is the inverter's voltage estimate unless TRUST_SOC is on, so any
  capacity implied from it is indicative. Watt-hours drawn are measured.

  The grid leg is derived from the energy balance, like everywhere else.

ASCII only: the Windows console will not render anything fancier.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

import bridge
import config

# A sample gap longer than this is missing data, not a flat line.
MAX_GAP_S = 30.0
# On-battery episodes closer together than this are one episode with a blip.
EPISODE_MERGE_S = 90.0
# A watts reading below this is "the sun is not up" for the sunrise/sunset times.
PV_LIVE_W = 30.0


# --------------------------------------------------------------------------
# reading the logs -- the samples themselves come from days.DayLog
# --------------------------------------------------------------------------

def load_energy_book() -> dict:
    try:
        return (json.loads(bridge.ENERGY_PATH.read_text(encoding="utf-8"))
                .get("days") or {})
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------
# one day
# --------------------------------------------------------------------------

def analyse_day(day: dt.date, samples: list[dict], counters: dict | None) -> dict:
    counters = counters or {}
    out: dict = {"date": day.isoformat(), "samples": len(samples)}
    if not samples:
        out["pv_wh"] = counters.get("pv_wh")
        out["load_wh"] = counters.get("load_wh")
        return out

    hours = [{"load_w": [], "pv_w": [], "on_battery": 0, "grid_absent": 0,
              "batt_v": [], "soc": None, "n": 0} for _ in range(24)]
    integ = {"pv": 0.0, "load": 0.0, "batt_out": 0.0, "batt_in": 0.0, "grid": 0.0}
    on_battery_s = outage_s = covered_s = 0.0
    peak_load = (0.0, None)
    peak_pv = (0.0, None)
    soc_max = (None, None)
    soc_min = (None, None)
    pv_first = pv_last = None
    charge_first = charge_last = None
    episodes: list[dict] = []
    outages: list[dict] = []
    current: dict | None = None
    outage: dict | None = None

    prev = None
    for sample in samples:
        t, f = sample["t"], sample["flow"]
        load, pv = f["load_w"], f["pv_w"]
        hour = hours[t.hour]
        hour["n"] += 1
        hour["load_w"].append(load)
        hour["pv_w"].append(pv)
        hour["batt_v"].append(f["batt_v"])
        if f.get("batt_soc") is not None:
            hour["soc"] = f["batt_soc"]
        if f["on_battery"]:
            hour["on_battery"] += 1
        if not f["grid_present"]:
            hour["grid_absent"] += 1

        if load > peak_load[0]:
            peak_load = (load, t)
        if pv > peak_pv[0]:
            peak_pv = (pv, t)
        soc = f.get("batt_soc")
        if soc is not None:
            if soc_max[0] is None or soc > soc_max[0]:
                soc_max = (soc, t)
            if soc_min[0] is None or soc < soc_min[0]:
                soc_min = (soc, t)
        if pv >= PV_LIVE_W:
            pv_first = pv_first or t
            pv_last = t
        if f["batt_charge_a"] > 0:
            charge_first = charge_first or t
            charge_last = t

        gap = (t - prev["t"]).total_seconds() if prev else 0.0
        if gap > EPISODE_MERGE_S:
            # A hole in the data closes whatever was running: the link was
            # down, and nothing is known about the pack in between.
            if current is not None:
                current["truncated"] = True
                episodes.append(current)
                current = None
            if outage is not None:
                outage["truncated"] = True
                outages.append(outage)
                outage = None
        if prev and 0 < gap <= MAX_GAP_S:
            h = gap / 3600.0
            pf = prev["flow"]
            integ["pv"] += pf["pv_w"] * h
            integ["load"] += pf["load_w"] * h
            integ["batt_out"] += pf["batt_discharge_w"] * h
            integ["batt_in"] += pf["batt_charge_w"] * h
            integ["grid"] += pf["grid_w"] * h
            covered_s += gap
            if pf["on_battery"]:
                on_battery_s += gap
                if current is not None:
                    current["wh"] += pf["batt_discharge_w"] * h
                    current["load_wh"] += pf["load_w"] * h
                    current["grid_present_s"] += gap if pf["grid_present"] else 0.0
            if not pf["grid_present"]:
                outage_s += gap

        # on-battery episodes, merged across short blips
        if f["on_battery"]:
            if current is None:
                current = {"start": t, "end": t, "wh": 0.0, "load_wh": 0.0,
                           "soc_start": soc, "soc_end": soc,
                           "v_start": f["batt_v"], "v_end": f["batt_v"],
                           "grid_present_s": 0.0, "peak_w": load,
                           "grid_at_start": f["grid_present"]}
            current["end"] = t
            current["soc_end"] = soc if soc is not None else current["soc_end"]
            current["v_end"] = f["batt_v"]
            current["peak_w"] = max(current["peak_w"], load)
        elif current is not None and (t - current["end"]).total_seconds() > EPISODE_MERGE_S:
            episodes.append(current)
            current = None

        # grid outages, by voltage
        if not f["grid_present"]:
            if outage is None:
                outage = {"start": t, "end": t}
            outage["end"] = t
        elif outage is not None and (t - outage["end"]).total_seconds() > EPISODE_MERGE_S:
            outages.append(outage)
            outage = None
        prev = sample

    if current is not None:
        current["open"] = True
        episodes.append(current)
    if outage is not None:
        outage["open"] = True
        outages.append(outage)

    for ep in episodes:
        ep["duration_s"] = (ep["end"] - ep["start"]).total_seconds()
        ep["avg_w"] = ep["wh"] / (ep["duration_s"] / 3600.0) if ep["duration_s"] > 0 else 0.0
        drop = (None if ep["soc_start"] is None or ep["soc_end"] is None
                else ep["soc_start"] - ep["soc_end"])
        ep["soc_drop"] = drop
        # What the pack would hold at 100% if the SOC scale were linear and
        # honest -- neither is guaranteed, which is why it is "implied".
        ep["implied_pack_wh"] = (round(ep["wh"] / (drop / 100.0))
                                 if drop and drop >= 5 else None)
        ep["grid_present_fraction"] = (ep["grid_present_s"] / ep["duration_s"]
                                       if ep["duration_s"] > 0
                                       else float(ep["grid_at_start"]))
        ep["kind"] = ("outage" if ep["grid_present_fraction"] < 0.5 else "by priority")
    for o in outages:
        o["duration_s"] = (o["end"] - o["start"]).total_seconds()

    out.update({
        "coverage_h": round(covered_s / 3600.0, 2),
        "pv_wh": counters.get("pv_wh"), "load_wh": counters.get("load_wh"),
        "pv_int_wh": round(integ["pv"]), "load_int_wh": round(integ["load"]),
        "batt_out_wh": round(integ["batt_out"]), "batt_in_wh": round(integ["batt_in"]),
        "grid_wh": round(integ["grid"]),
        "on_battery_s": round(on_battery_s), "outage_s": round(outage_s),
        "peak_load_w": peak_load[0], "peak_load_at": _hm(peak_load[1]),
        "peak_pv_w": peak_pv[0], "peak_pv_at": _hm(peak_pv[1]),
        "pv_first": _hm(pv_first), "pv_last": _hm(pv_last),
        "charge_first": _hm(charge_first), "charge_last": _hm(charge_last),
        "soc_max": soc_max[0], "soc_max_at": _hm(soc_max[1]),
        "soc_min": soc_min[0], "soc_min_at": _hm(soc_min[1]),
        "hours": [{
            "hour": i, "n": h["n"],
            "load_w": round(statistics.fmean(h["load_w"])) if h["n"] else None,
            "pv_w": round(statistics.fmean(h["pv_w"])) if h["n"] else None,
            "batt_v": round(statistics.fmean(h["batt_v"]), 2) if h["n"] else None,
            "soc": h["soc"],
            "on_battery": round(h["on_battery"] / h["n"], 2) if h["n"] else None,
            "grid_absent": round(h["grid_absent"] / h["n"], 2) if h["n"] else None,
        } for i, h in enumerate(hours)],
        "episodes": [{
            "start": _hm(e["start"]), "end": _hm(e["end"]),
            "duration_s": round(e["duration_s"]), "kind": e["kind"],
            "wh": round(e["wh"]), "avg_w": round(e["avg_w"]), "peak_w": e["peak_w"],
            "load_wh": round(e["load_wh"]),
            "soc_start": e["soc_start"], "soc_end": e["soc_end"],
            "soc_drop": e["soc_drop"], "v_start": e["v_start"], "v_end": e["v_end"],
            "implied_pack_wh": e["implied_pack_wh"], "open": e.get("open", False),
            "truncated": e.get("truncated", False),
        } for e in episodes],
        "outages": [{"start": _hm(o["start"]), "end": _hm(o["end"]),
                     "duration_s": round(o["duration_s"]), "open": o.get("open", False),
                     "truncated": o.get("truncated", False)}
                    for o in outages],
    })
    return out


def _hm(t: dt.datetime | None) -> str | None:
    return None if t is None else t.strftime("%H:%M")


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _kwh(wh) -> str:
    return "  --  " if wh is None else f"{wh / 1000:5.1f}"


def _dur(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 3600:
        return f"{seconds // 60:d} min"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _bar(value, full, width) -> str:
    if not value or not full:
        return "." * width
    n = max(1, min(width, int(round(width * value / full))))
    return "#" * n + "." * (width - n)


def render_day(day: dict, *, hourly: bool = True) -> str:
    out = []
    rule = "-" * 78
    out += [rule, f" {day['date']}   {day['samples']} samples, "
                  f"{day.get('coverage_h', 0):.1f} h covered", rule]
    if not day["samples"]:
        out.append(f"  no log for this day"
                   + (f"   (inverter counters: solar {_kwh(day.get('pv_wh'))} kWh,"
                      f" load {_kwh(day.get('load_wh'))} kWh)"
                      if day.get("pv_wh") is not None else ""))
        return "\n".join(out)

    pv_src = "inverter" if day["pv_wh"] is not None else "integrated"
    pv_wh = day["pv_wh"] if day["pv_wh"] is not None else day["pv_int_wh"]
    load_wh = day["load_wh"] if day["load_wh"] is not None else day["load_int_wh"]
    out.append(f"  ENERGY   solar {_kwh(pv_wh)} kWh ({pv_src})   load {_kwh(load_wh)} kWh"
               f"   grid {_kwh(day['grid_wh'])} kWh (derived)")
    out.append(f"           battery out {_kwh(day['batt_out_wh'])} kWh   "
               f"in {_kwh(day['batt_in_wh'])} kWh   "
               f"on battery {_dur(day['on_battery_s'])}   grid absent {_dur(day['outage_s'])}")
    out.append(f"  PEAKS    load {day['peak_load_w']:.0f} W at {day['peak_load_at']}"
               f"   solar {day['peak_pv_w']:.0f} W at {day['peak_pv_at'] or '--'}"
               f"   sun {day['pv_first'] or '--'} to {day['pv_last'] or '--'}")
    soc_line = "  BATTERY  "
    if day["soc_max"] is not None:
        soc_line += (f"fullest {day['soc_max']}% at {day['soc_max_at']}   "
                     f"lowest {day['soc_min']}% at {day['soc_min_at']}   ")
    soc_line += f"charging {day['charge_first'] or '--'} to {day['charge_last'] or '--'}"
    out.append(soc_line)

    if day["episodes"]:
        out.append("  ON BATTERY")
        for e in day["episodes"]:
            soc = (f"{e['soc_start']}% -> {e['soc_end']}%" if e["soc_drop"] is not None
                   else "soc --")
            implied = (f"   ~{e['implied_pack_wh'] / 1000:.1f} kWh pack implied"
                       if e["implied_pack_wh"] else "")
            mark = "+" if e["open"] else "?" if e["truncated"] else " "
            out.append(f"    {e['start']}-{e['end']}{mark} "
                       f"{_dur(e['duration_s']):>8}  {e['kind']:<11} "
                       f"{e['wh']:5.0f} Wh drawn  avg {e['avg_w']:4.0f} W  "
                       f"{soc}  {e['v_start']:.1f}->{e['v_end']:.1f} V{implied}")
        if any(e["open"] or e["truncated"] for e in day["episodes"]):
            out.append("    (+ still running at the end of the log, ? cut short by a gap in the data)")
    if day["outages"]:
        out.append("  GRID OUT  " + "   ".join(
            f"{o['start']}-{o['end']}{'+' if o['open'] else '?' if o['truncated'] else ''}"
            f" ({_dur(o['duration_s'])})" for o in day["outages"]))

    if hourly:
        peak_load = max((h["load_w"] or 0) for h in day["hours"]) or 1
        peak_pv = max((h["pv_w"] or 0) for h in day["hours"]) or 1
        out.append("")
        out.append("  hour   load W              solar W             batt V  soc  on-batt")
        for h in day["hours"]:
            if not h["n"]:
                out.append(f"  {h['hour']:02d}     (no data)")
                continue
            flag = ("B" * int(round((h["on_battery"] or 0) * 4))).ljust(4, ".")
            out.append(f"  {h['hour']:02d}  {_bar(h['load_w'], peak_load, 12)} {h['load_w']:5d}"
                       f"   {_bar(h['pv_w'], peak_pv, 12)} {h['pv_w']:5d}"
                       f"   {h['batt_v']:5.2f}  {h['soc'] if h['soc'] is not None else '--':>3}"
                       f"   {flag}")
    return "\n".join(out)


def render_summary(days: list[dict]) -> str:
    """Across the run: the regularities worth planning against."""
    with_data = [d for d in days if d["samples"]]
    out = ["=" * 78, " ACROSS THE RUN", "=" * 78]
    if not with_data:
        out.append("  nothing logged yet")
        return "\n".join(out)

    # the shape of a typical day, hour by hour
    load_by_hour = [[] for _ in range(24)]
    pv_by_hour = [[] for _ in range(24)]
    batt_by_hour = [[] for _ in range(24)]
    for d in with_data:
        for h in d["hours"]:
            if h["n"]:
                load_by_hour[h["hour"]].append(h["load_w"])
                pv_by_hour[h["hour"]].append(h["pv_w"])
                batt_by_hour[h["hour"]].append(h["on_battery"])
    load_prof = [statistics.fmean(v) if v else None for v in load_by_hour]
    pv_prof = [statistics.fmean(v) if v else None for v in pv_by_hour]
    batt_prof = [statistics.fmean(v) if v else None for v in batt_by_hour]
    have = [i for i in range(24) if load_prof[i] is not None]
    if have:
        top = sorted(have, key=lambda i: -load_prof[i])[:3]
        out.append("  usage peaks at   " + ", ".join(
            f"{i:02d}:00 ({load_prof[i]:.0f} W)" for i in sorted(top)))
        sunny = [i for i in have if pv_prof[i] and pv_prof[i] >= PV_LIVE_W]
        if sunny:
            best = max(sunny, key=lambda i: pv_prof[i])
            out.append(f"  solar carries    {sunny[0]:02d}:00 to {sunny[-1] + 1:02d}:00, "
                       f"best hour {best:02d}:00 ({pv_prof[best]:.0f} W)")
        on_batt = [i for i in have if batt_prof[i] and batt_prof[i] >= 0.5]
        if on_batt:
            out.append("  on battery mostly " + _runs(on_batt))

    # the battery question
    eps = [e for d in with_data for e in d["episodes"]
           if not e["open"] and not e["truncated"]]
    implied = [e["implied_pack_wh"] for e in eps if e["implied_pack_wh"]]
    out.append(f"  battery episodes {len(eps)}"
               + (f"   of which grid was present for {sum(1 for e in eps if e['kind'] == 'by priority')}"
                  " (priority setting, not an outage)" if eps else ""))
    if implied:
        out.append(f"  implied pack     median {statistics.median(implied) / 1000:.1f} kWh "
                   f"per 100% SOC over {len(implied)} episodes with a 5%+ drop"
                   f"   (range {min(implied) / 1000:.1f} to {max(implied) / 1000:.1f})")
        if config.BATTERY_CAPACITY_AH:
            rated = config.BATTERY_CAPACITY_AH * 51.2
            out.append(f"  rated pack       {rated / 1000:.1f} kWh "
                       f"({config.BATTERY_CAPACITY_AH:.0f} Ah x 51.2 V) -- "
                       f"implied is {statistics.median(implied) / rated * 100:.0f}% of it")
        elif config.BATTERY_SOLD_WH:
            out.append(f"  sold as          {config.BATTERY_SOLD_WH / 1000:.1f} kWh -- "
                       f"implied is {statistics.median(implied) / config.BATTERY_SOLD_WH * 100:.0f}% of it;"
                       f" the plan caps the pack at {config.BATTERY_PACK_WH_CAP / 1000:.1f} kWh"
                       f" (what it delivers in practice, Oracle #29)")
        else:
            out.append("  rated pack       set config.BATTERY_CAPACITY_AH to compare")
    out.append("  soc caveat       the SOC is the inverter's voltage estimate; watt-hours "
               "drawn are measured, the percentages are not")

    pv_days = [d for d in with_data if d["pv_wh"]]
    if len(pv_days) >= 2:
        vals = [d["pv_wh"] for d in pv_days]
        out.append(f"  solar per day    median {statistics.median(vals) / 1000:.1f} kWh, "
                   f"best {max(vals) / 1000:.1f}, worst {min(vals) / 1000:.1f} "
                   f"over {len(vals)} days with an inverter counter")
    return "\n".join(out)


def _runs(hours: list[int]) -> str:
    runs, start, prev = [], hours[0], hours[0]
    for h in hours[1:]:
        if h != prev + 1:
            runs.append((start, prev))
            start = h
        prev = h
    runs.append((start, prev))
    return ", ".join(f"{a:02d}:00-{b + 1:02d}:00" for a, b in runs)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=7, help="how many days back (default 7)")
    parser.add_argument("--date", help="one day, YYYY-MM-DD")
    parser.add_argument("--no-hourly", action="store_true", help="skip the hour tables")
    parser.add_argument("--out", metavar="PATH", help="write the report here as well")
    parser.add_argument("--json", metavar="PATH", help="write the numbers here")
    args = parser.parse_args()

    today = bridge.now_site().date()
    if args.date:
        dates = [dt.date.fromisoformat(args.date)]
    else:
        dates = [today - dt.timedelta(days=back) for back in range(args.days - 1, -1, -1)]

    import days as daysmod                  # days imports this module
    store = daysmod.DayStore()
    days = [store.day(day) for day in dates]

    text = "\n\n".join(render_day(d, hourly=not args.no_hourly) for d in days)
    if len(days) > 1:
        text += "\n\n" + render_summary(days)
    print(text)
    if args.out:
        pathlib.Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(days, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
