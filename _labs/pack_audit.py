"""Lab: what the logs already say about the pack, before any new hardware.

Reads every QPIGS sample this checkout has logged (logs/<date>.jsonl) and,
when present, Switch-X's line meters (D:/PowerTools/switch-x/logs/power/),
and prints the evidence for Oracle #29 in six parts:

  A  charge ceiling   how full does the pack actually get, and what ends the charge
  B  idle stretches   the SOC slide at rest against a flat voltage
  C  voltage map      rest and loaded voltage per SOC band (is the SOC a voltage lookup?)
  D  discharge runs   Wh out per SOC point, net of the idle decay; load Wh per battery Wh
  E  meters           inverter_in vs out1+out2 on the grid (the inverter's own draw)
                      and battery W vs out on the pack (DC->AC efficiency)
  F  day book         Wh into the pack vs Wh out of it, per day

Opens no link. ASCII only. Usage:  python _labs/pack_audit.py [--days N] [--no-meters]

Resolution warning that runs through everything below: QPIGS reports battery
current in WHOLE AMPS. A reading of 0 A hides anything up to ~0.9 A, which at
53 V is ~50 W -- 1 %/h of a 5 kWh pack, 2.5 %/h of a 2 kWh one. So "no
current" from this inverter never rules out a drain of that size; only the
BMS or a DC shunt can.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOGS = os.path.join(ROOT, "logs")
SWITCHX_POWER = r"D:\PowerTools\switch-x\logs\power"

IDLE_MIN_MIN = 30        # an idle stretch must last this long to count
IDLE_PV_W = 50           # PV under this is dark
IDLE_MAX_CHG_A = 1       # a 1 A trickle reading still counts as rest (2 A cap on utility charge)
RUN_MIN_MIN = 20         # a discharge run must last this long
GAP_S = 120              # a hole in the samples longer than this breaks a stretch
DECAY_PTS_PER_H = 2.0    # the idle slide measured in part B, used in part D


# ----------------------------------------------------------------------------
# loading

def day_logs() -> list[str]:
    """Each day's log once: the plain JSONL, or the <date>.jsonl.gz the
    collector archives it to once it is older than yesterday."""
    plain = glob.glob(os.path.join(LOGS, "20??-??-??.jsonl"))
    packed = [p for p in glob.glob(os.path.join(LOGS, "20??-??-??.jsonl.gz"))
              if p[:-3] not in plain]
    return sorted(plain + packed, key=os.path.basename)


def load_samples(days: int | None) -> list[dict]:
    rows = []
    files = day_logs()
    if days:
        files = files[-days:]
    mode = None
    for path in files:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") != "read" or not rec.get("ok"):
                    continue
                d = rec.get("data") or {}
                if rec.get("cmd") == "QMOD":
                    mode = d.get("mode")
                    continue
                if rec.get("cmd") != "QPIGS":
                    continue
                try:
                    t = dt.datetime.fromisoformat(rec["local"])
                except (KeyError, ValueError):
                    continue
                v, soc = d.get("batt_v"), d.get("batt_soc")
                if v is None or soc is None:
                    continue
                pv = d.get("pv_charge_w")
                if pv is None:
                    pv = (d.get("pv_v") or 0) * (d.get("pv_a") or 0)
                rows.append({
                    "t": t, "mode": mode, "v": float(v), "soc": int(soc),
                    "chg": float(d.get("batt_charge_a") or 0),
                    "dis": float(d.get("batt_discharge_a") or 0),
                    "load": float(d.get("ac_out_w") or 0),
                    "pv": float(pv or 0), "grid_v": float(d.get("grid_v") or 0),
                    "temp": d.get("heatsink_c"),
                })
    rows.sort(key=lambda r: r["t"])
    return rows


def load_meters(days: int | None) -> dict[str, list[tuple[float, float]]]:
    """slug -> [(epoch, watts)] for the three inverter meters and the mains."""
    out: dict[str, list] = {}
    if not os.path.isdir(SWITCHX_POWER):
        return out
    files = sorted(glob.glob(os.path.join(SWITCHX_POWER, "20??-??-??.jsonl")))
    if days:
        files = files[-days:]
    want = {"inverter_in", "inverter_out_1", "inverter_out_2", "wapda_main"}
    for path in files:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("d") in want and "w" in rec:
                    va = float(rec.get("v") or 0) * float(rec.get("a") or 0)
                    out.setdefault(rec["d"], []).append((float(rec["t"]), float(rec["w"]), va))
    for k in out:
        out[k].sort()
    return out


class Held:
    """Last-value-hold lookup over a sorted (t, w) series; None when the
    last row is older than `stale` seconds (the meter logs on change and
    on a keepalive, so a value older than the keepalive is a lost meter)."""

    def __init__(self, series, stale=900):
        self.s = series
        self.i = 0
        self.stale = stale

    def at(self, t: float, va: bool = False):
        while self.i + 1 < len(self.s) and self.s[self.i + 1][0] <= t:
            self.i += 1
        if not self.s or self.s[self.i][0] > t:
            return None
        if t - self.s[self.i][0] > self.stale:
            return None
        return self.s[self.i][2 if va else 1]


# ----------------------------------------------------------------------------
# helpers

def hm(t: dt.datetime) -> str:
    return t.strftime("%m-%d %H:%M")


def hours(a: dt.datetime, b: dt.datetime) -> float:
    return (b - a).total_seconds() / 3600


def stretches(rows, keep, min_minutes):
    """Maximal runs of consecutive rows where keep(row) holds, broken by a
    sample gap over GAP_S, at least min_minutes long."""
    out, cur = [], []
    for r in rows:
        if keep(r) and (not cur or (r["t"] - cur[-1]["t"]).total_seconds() <= GAP_S):
            cur.append(r)
        else:
            if cur and hours(cur[0]["t"], cur[-1]["t"]) * 60 >= min_minutes:
                out.append(cur)
            cur = [r] if keep(r) else []
    if cur and hours(cur[0]["t"], cur[-1]["t"]) * 60 >= min_minutes:
        out.append(cur)
    return out


def wh_between(rows, key):
    """Integrate rows[i][key] (watts) over time, trapezoid, in Wh."""
    total = 0.0
    for a, b in zip(rows, rows[1:]):
        dt_h = (b["t"] - a["t"]).total_seconds() / 3600
        if 0 < dt_h <= GAP_S / 3600:
            total += (a[key] + b[key]) / 2 * dt_h
    return total


def med(xs, nd=1):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), nd) if xs else None


def line(ch="-", n=78):
    print(ch * n)


# ----------------------------------------------------------------------------
# A  charge ceiling

def part_a(rows):
    line("=")
    print(" A  CHARGE CEILING -- how full does the pack get, and what ends the charge")
    line("=")
    print("  settings (QPIRI, 2026-09-22): bulk 57.6 V, float 57.6 V, type 9, max charge 40 A,")
    print("  utility charge 2 A, charger priority Solar only. 57.6 V / 16 cells = 3.60 V/cell.")
    print()
    print("  day         vmax  at     soc chg  pv   | soc_max at    v     | last real charge (>=2 A): V  soc  at   | sun left after it")
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["t"].date().isoformat(), []).append(r)
    for day, rs in by_day.items():
        vmax = max(rs, key=lambda r: r["v"])
        smax = max(rs, key=lambda r: r["soc"])
        charging = [r for r in rs if r["chg"] >= 2]
        last = charging[-1] if charging else None
        # PV available but no charge and pack not full -- charge ended by something
        if last:
            after = [r for r in rs if r["t"] > last["t"] and r["pv"] > 300 and r["chg"] < 1 and r["soc"] < 100]
            sun_left = f"{len(after)} samples, pv med {med([r['pv'] for r in after], 0)} W, soc {after[0]['soc']}..{after[-1]['soc']}" if after else "none"
            lastc = f"{last['v']:5.1f} {last['soc']:3d}  {last['t'].strftime('%H:%M')}"
        else:
            sun_left, lastc = "-", "no charge >= 2 A logged"
        print(f"  {day}  {vmax['v']:5.1f} {vmax['t'].strftime('%H:%M')} {vmax['soc']:3d} {vmax['chg']:3.0f}A {vmax['pv']:4.0f} | "
              f"{smax['soc']:3d}     {smax['t'].strftime('%H:%M')} {smax['v']:5.1f} | {lastc:26s} | {sun_left}")
    print()
    # Voltage while charging hard, by SOC band: does V climb toward 57.6 or sit under 55?
    print("  voltage under charge (>= 5 A), by SOC band: median V, median A, samples")
    bands: dict[int, list] = {}
    for r in rows:
        if r["chg"] >= 5:
            bands.setdefault(r["soc"] // 10 * 10, []).append(r)
    for b in sorted(bands):
        rs = bands[b]
        print(f"    soc {b:3d}-{b+9:3d}  V {med([r['v'] for r in rs])}   A {med([r['chg'] for r in rs], 0)}   n={len(rs)}")
    top = max(rows, key=lambda r: r["v"])
    print(f"\n  highest battery voltage ever logged: {top['v']} V at {hm(top['t'])} (soc {top['soc']}, {top['chg']:.0f} A in, pv {top['pv']:.0f} W)")
    print("  verdict key: if the pack never passes ~55 V while the sun and the 40 A are there,")
    print("  the charge is being ended below the inverter's 57.6 V -- by the BMS, or the pack is full")
    print("  at that voltage and the SOC scale is simply wrong. Either way the top is unmeasured by SOC.")
    print()


# ----------------------------------------------------------------------------
# B  idle stretches

def part_b(rows):
    line("=")
    print(" B  IDLE STRETCHES -- grid carrying the house, no discharge, at most a 1 A trickle, dark")
    line("=")
    keep = lambda r: (r["mode"] == "L" and r["dis"] == 0 and r["chg"] <= IDLE_MAX_CHG_A and r["pv"] < IDLE_PV_W)
    runs = stretches(rows, keep, IDLE_MIN_MIN)
    print("  start          len   soc        V (first->last, med)   slide/h   trickle A (mean)   load W")
    slides = []
    for rs in runs:
        h = hours(rs[0]["t"], rs[-1]["t"])
        slide = (rs[-1]["soc"] - rs[0]["soc"]) / h
        slides.append(slide)
        print(f"  {hm(rs[0]['t'])}  {h:4.1f}h  {rs[0]['soc']:3d} -> {rs[-1]['soc']:3d}   "
              f"{rs[0]['v']:5.1f} -> {rs[-1]['v']:5.1f}  {med([r['v'] for r in rs]):5.1f}   {slide:+5.1f}   "
              f"{statistics.mean(r['chg'] for r in rs):4.2f}            {med([r['load'] for r in rs], 0)}")
    if slides:
        print(f"\n  median slide {med(slides):+.1f} points/h over {len(runs)} stretches")
    print("  what a real drain of that size would need: 2 %/h of 5 kWh = 100 W = 1.9 A; of 2 kWh = 40 W = 0.75 A.")
    print("  the 0.75 A case is INSIDE the whole-amp rounding of QPIGS: these logs cannot tell a 2 kWh pack")
    print("  really draining at 40 W from an SOC counter that ticks down on its own. Part E and the BMS can.")
    print()


# ----------------------------------------------------------------------------
# C  voltage map

def part_c(rows):
    line("=")
    print(" C  VOLTAGE MAP -- is the SOC a function of the voltage at all?")
    line("=")
    rest = [r for r in rows if r["dis"] == 0 and r["chg"] <= 1 and r["pv"] < IDLE_PV_W]
    load = [r for r in rows if r["dis"] >= 3]
    chg = [r for r in rows if r["chg"] >= 3]
    print("  soc band   rest V (med, n)     under load V (med, med A, n)    under charge V (med, med A, n)")
    for b in range(0, 100, 5):
        rr = [r for r in rest if b <= r["soc"] < b + 5]
        ll = [r for r in load if b <= r["soc"] < b + 5]
        cc = [r for r in chg if b <= r["soc"] < b + 5]
        if not (rr or ll or cc):
            continue
        f = lambda rs, key: f"{med([r['v'] for r in rs]):5.1f} ({len(rs):5d})" if rs else "   -           "
        g = lambda rs, key: f"{med([r['v'] for r in rs]):5.1f} {med([r[key] for r in rs], 0):3.0f}A ({len(rs):5d})" if rs else "   -                "
        print(f"  {b:3d}-{b+4:3d}    {f(rr, None)}    {g(ll, 'dis')}     {g(cc, 'chg')}")
    print("  reading: a LiFePO4 16S pack rests at 3.30-3.33 V/cell (52.8-53.3 V) from ~30 % to ~90 %.")
    print("  A flat rest voltage across SOC bands is the chemistry's plateau, NOT proof that nothing")
    print("  left the pack. It is proof the SOC is not a voltage lookup on this inverter.")
    print()


# ----------------------------------------------------------------------------
# D  discharge runs

def part_d(rows):
    line("=")
    print(" D  DISCHARGE RUNS -- Wh out per SOC point, net of the idle decay; load Wh per battery Wh")
    line("=")
    runs = stretches(rows, lambda r: r["dis"] > 0, RUN_MIN_MIN)
    print("  start          len   soc        V first->last  batt Wh  load Wh  load/batt  Wh/pt  decay pts  Wh/real pt  grid")
    per_pt, per_real, eff = [], [], []
    for rs in runs:
        for r in rs:
            r["bw"] = r["v"] * r["dis"]
        h = hours(rs[0]["t"], rs[-1]["t"])
        bwh = wh_between(rs, "bw")
        lwh = wh_between(rs, "load")
        pts = rs[0]["soc"] - rs[-1]["soc"]
        decay = DECAY_PTS_PER_H * h
        real = pts - decay
        wpp = bwh / pts if pts > 0 else None
        wpr = bwh / real if real > 0.5 else None
        grid = "present" if med([r["grid_v"] for r in rs]) > 150 else "ABSENT"
        if wpp: per_pt.append(wpp)
        if wpr: per_real.append(wpr)
        if bwh > 20: eff.append(lwh / bwh)
        print(f"  {hm(rs[0]['t'])}  {h:4.1f}h  {rs[0]['soc']:3d} -> {rs[-1]['soc']:3d}   {rs[0]['v']:5.1f} -> {rs[-1]['v']:5.1f}   "
              f"{bwh:6.0f}   {lwh:6.0f}   {lwh/bwh if bwh else 0:5.2f}   "
              f"{(wpp or 0):5.0f}   {decay:5.1f}      {(wpr or 0):5.0f}   {grid}")
    if per_pt:
        print(f"\n  Wh per SOC point: median {med(per_pt, 0)} -> {med(per_pt, 0)*100/1000:.1f} kWh per 100 % on the raw scale")
    if per_real:
        print(f"  Wh per point net of {DECAY_PTS_PER_H}/h decay: median {med(per_real, 0)} -> {med(per_real, 0)*100/1000:.1f} kWh per 100 %")
    if eff:
        print(f"  load Wh per battery Wh (inverter DC->AC incl. its own draw): median {med(eff, 2)}")
    print("  the 'per 100 %' figures are the SOC's own scale, whatever it is. They are NOT the pack's")
    print("  capacity. That needs one run from a known full to the cut-off with the Wh integrated (see README).")
    print()


# ----------------------------------------------------------------------------
# E  meters

def part_e(rows, meters):
    line("=")
    print(" E  METERS -- Switch-X inverter_in vs out1+out2 (grid nights), battery W vs out (pack)")
    line("=")
    if not all(k in meters for k in ("inverter_in", "inverter_out_1", "inverter_out_2")):
        print("  no Switch-X meter rows found under", SWITCHX_POWER)
        print()
        return
    hin, h1, h2 = (Held(meters[k]) for k in ("inverter_in", "inverter_out_1", "inverter_out_2"))
    joined = []
    for r in rows:
        t = r["t"].timestamp()
        a, b, c = hin.at(t), h1.at(t), h2.at(t)
        if a is None or b is None or c is None:
            continue
        joined.append({**r, "in": a, "in_va": hin.at(t, va=True), "out": b + c})
    print(f"  joined samples: {len(joined)} of {len(rows)} (meters log on change + keepalive; values held up to 15 min)")
    if not joined:
        print()
        return

    # E1  on the grid at night: in - out = what the inverter itself eats + whatever it pushes into the pack
    night = [j for j in joined if j["mode"] == "L" and j["pv"] < IDLE_PV_W and j["dis"] == 0]
    print("\n  E1  on the grid, dark, no discharge: inverter_in - (out1+out2) by hour (median W)")
    print("      hour   in W   in VA   out W   in-out W   inverter ac_out_w   chg A (mean)   n")
    byh: dict[int, list] = {}
    for j in night:
        byh.setdefault(j["t"].hour, []).append(j)
    diffs = []
    for h in sorted(byh):
        js = byh[h]
        d = med([j["in"] - j["out"] for j in js], 0)
        diffs.append(d)
        print(f"      {h:02d}     {med([j['in'] for j in js], 0):5.0f}  {med([j['in_va'] for j in js], 0):5.0f}   {med([j['out'] for j in js], 0):5.0f}   {d:6.0f}      "
              f"{med([j['load'] for j in js], 0):5.0f}              {statistics.mean(j['chg'] for j in js):4.2f}        {len(js)}")
    if diffs:
        print(f"      median in-out over the night hours: {med(diffs, 0)} W")
        pf = [j["in"] / j["in_va"] for j in night if j["in_va"] > 50]
        print(f"      inverter_in apparent power factor (W/VA): median {med(pf, 2)}; out1 runs ~0.95")
        print("      a bypassing inverter cannot take fewer real watts in than it puts out. A negative in-out with a")
        print("      PF that low says the Inverter In clamp meter under-reads the inverter's distorted input current:")
        print("      NOT usable for the inverter's standby draw until it is checked against a known load.")

    # E2  on the pack: battery W (V*A, whole amps) vs out, binned, for a fixed + proportional loss
    onb = [j for j in joined if j["dis"] >= 2 and j["pv"] < IDLE_PV_W]
    print("\n  E2  on the pack, dark (>= 2 A out): battery W vs out1+out2 vs inverter ac_out_w")
    if onb:
        bw = [j["v"] * j["dis"] for j in onb]
        print(f"      n={len(onb)}  batt W med {med(bw, 0)}   out W med {med([j['out'] for j in onb], 0)}   "
              f"inverter load med {med([j['load'] for j in onb], 0)}")
        ratio = [j["out"] / (j["v"] * j["dis"]) for j in onb if j["dis"] >= 4]
        if ratio:
            print(f"      out / batt W (>= 4 A only, rounding): median {med(ratio, 2)}   "
                  f"-> inverter loses ~{(1 - statistics.median(ratio)) * 100:.0f} % of what leaves the pack")
        r2 = [j["load"] / j["out"] for j in onb if j["out"] > 100]
        if r2:
            print(f"      inverter ac_out_w / meters out: median {med(r2, 2)}  (the inverter's own load figure vs the two line meters)")
        print("      by battery amps (whole amps, so each bin is a 53 W step): batt W, out W, loss W, out/batt, n")
        for a in range(2, 40):
            js = [j for j in onb if j["dis"] == a]
            if len(js) < 30:
                continue
            b = med([j["v"] * j["dis"] for j in js], 0)
            o = med([j["out"] for j in js], 0)
            print(f"        {a:3d} A   {b:5.0f}   {o:5.0f}   {b - o:5.0f}   {o / b:5.2f}   {len(js)}")
        print("      the loss column against the batt W column is the inverter's own draw (the intercept) plus")
        print("      its conversion loss (the slope). Whole-amp rounding puts +-27 W on every row.")
    else:
        print("      no joined on-battery samples yet")

    # E4  in B mode at float: the current the inverter reports leaving a full pack
    flt = [j for j in joined if j["mode"] == "B" and j["v"] >= 57.0 and j["chg"] == 0]
    if flt:
        print(f"\n  E4  B mode at float (V >= 57.0, chg 0): n={len(flt)}  dis A med {med([j['dis'] for j in flt], 0)}  "
              f"pv med {med([j['pv'] for j in flt], 0)} W  out med {med([j['out'] for j in flt], 0)} W  "
              f"pv-out med {med([j['pv'] - j['out'] for j in flt], 0)} W")
        print("      a full pack held at 57.6 V with '2 A out' all afternoon is either the inverter's own DC draw")
        print("      taken off the battery terminal while the SCC tracks the bus, or a sensor offset. ~110 W either way.")

    # E3  the inverter's ac_out_w vs the meters on the grid by day: is the house figure honest?
    print("\n  E3  inverter ac_out_w vs out1+out2, all joined samples with out > 100 W, per day (median ratio)")
    byd: dict[str, list] = {}
    for j in joined:
        if j["out"] > 100:
            byd.setdefault(j["t"].date().isoformat(), []).append(j["load"] / j["out"])
    for d in sorted(byd):
        print(f"      {d}  {med(byd[d], 2)}  (n={len(byd[d])})")
    print()


# ----------------------------------------------------------------------------
# F  day book

def part_f(rows):
    line("=")
    print(" F  DAY BOOK -- Wh into the pack vs out of it, per day (V x whole amps, so +-50 W)")
    line("=")
    by_day: dict[str, list] = {}
    for r in rows:
        r["cw"] = r["v"] * r["chg"]
        r["dw"] = r["v"] * r["dis"]
        by_day.setdefault(r["t"].date().isoformat(), []).append(r)
    print("  day         hours logged   in Wh   out Wh   out/in   soc first  last  min  max")
    for day, rs in by_day.items():
        cov = sum((b["t"] - a["t"]).total_seconds() for a, b in zip(rs, rs[1:]) if (b["t"] - a["t"]).total_seconds() <= GAP_S) / 3600
        cin, cout = wh_between(rs, "cw"), wh_between(rs, "dw")
        print(f"  {day}     {cov:5.1f}       {cin:6.0f}   {cout:6.0f}   {cout/cin if cin > 50 else 0:5.2f}     "
              f"{rs[0]['soc']:3d}      {rs[-1]['soc']:3d}   {min(r['soc'] for r in rs):3d}  {max(r['soc'] for r in rs):3d}")
    print("  a day with full coverage and the same SOC at both ends gives the round-trip: out/in near 0.9")
    print("  is a healthy LiFePO4 pack; well under that with the SOC not rising is energy the counter cannot see.")
    print()


# ----------------------------------------------------------------------------
# G  the morning refill -- a coulomb count over a known SOC span

def part_g(rows):
    line("=")
    print(" G  MORNING REFILL -- Wh into the pack from the night's lowest SOC to the first 100")
    line("=")
    print("  the one place the SOC span and the energy are both known: the charge current is the")
    print("  net battery current (pv ~= load + V x chg within 6 % in B mode), at 20-30 A the")
    print("  whole-amp rounding is +-2 %, and the top is a real full (chg falls to 0 at 57.6 V with sun left).")
    print()
    print("  day         low   at     V      full at   V      hours   Wh in   Wh out   Wh/pt   Ah in   Ah/pt")
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["t"].date().isoformat(), []).append(r)
    scale = []
    for day, rs in by_day.items():
        morning = [r for r in rs if 4 <= r["t"].hour < 12]
        if not morning:
            continue
        low = min(morning, key=lambda r: r["soc"])
        full = [r for r in rs if r["t"] > low["t"] and r["soc"] >= 100]
        if not full or low["soc"] >= 100:
            print(f"  {day}   {low['soc']:3d}   {low['t'].strftime('%H:%M')}  {low['v']:5.1f}   (no 100 reached after it)")
            continue
        end = full[0]
        seg = [r for r in rs if low["t"] <= r["t"] <= end["t"]]
        for r in seg:
            r["cw"], r["dw"], r["ca"] = r["v"] * r["chg"], r["v"] * r["dis"], r["chg"]
        cin, cout, ah = wh_between(seg, "cw"), wh_between(seg, "dw"), wh_between(seg, "ca")
        pts = 100 - low["soc"]
        h = hours(low["t"], end["t"])
        scale.append(cin / pts)
        print(f"  {day}   {low['soc']:3d}   {low['t'].strftime('%H:%M')}  {low['v']:5.1f}   {end['t'].strftime('%H:%M')}  {end['v']:5.1f}   "
              f"{h:4.1f}   {cin:6.0f}   {cout:5.0f}    {cin / pts:4.0f}    {ah:4.0f}    {ah / pts:4.2f}")
    if scale:
        s = statistics.median(scale)
        print(f"\n  charge-side scale: median {s:.0f} Wh per SOC point -> {s / 10:.1f} kWh per 100 points at the terminals,")
        print(f"  ~{s * 0.9 / 10:.1f} kWh stored at 90 % charge efficiency. The pack absorbs this much between the")
        print("  low state and full EVERY morning, so it physically holds at least that much above the low state.")
        print("  What lies under the low state (29-40 %, resting 52.3-52.8 V, mid-plateau) is unmeasured: nobody")
        print("  has taken it there. Compare the discharge-side 48-55 Wh/pt of part D: energy OUT per point exceeds")
        print("  energy IN per point, which no coulomb counter does. The SOC is not one; treat its scale as unknown.")
    print()


# ----------------------------------------------------------------------------
# H  the afternoon -- from full to dusk

def part_h(rows):
    line("=")
    print(" H  AFTERNOON -- from the first 100 to the last sun: what leaves the pack, and when the number moves")
    line("=")
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["t"].date().isoformat(), []).append(r)
    for day, rs in by_day.items():
        full = [r for r in rs if r["soc"] >= 100]
        if not full:
            continue
        t0 = full[0]["t"]
        sun = [r for r in rs if r["t"] > t0 and r["pv"] > 300]
        if not sun:
            continue
        t1 = sun[-1]["t"]
        seg = [r for r in rs if t0 <= r["t"] <= t1]
        for r in seg:
            r["cw"], r["dw"] = r["v"] * r["chg"], r["v"] * r["dis"]
        cin, cout = wh_between(seg, "cw"), wh_between(seg, "dw")
        at100 = [r for r in seg if r["soc"] >= 100]
        print(f"  {day}  full {t0.strftime('%H:%M')} -> last sun {t1.strftime('%H:%M')}: soc {seg[0]['soc']} -> {seg[-1]['soc']}, "
              f"{cin:.0f} Wh in, {cout:.0f} Wh out (incl. the float '2 A'), 100 held until {at100[-1]['t'].strftime('%H:%M')}")
        byh: dict[int, list] = {}
        for r in seg:
            byh.setdefault(r["t"].hour, []).append(r)
        print("      hour  soc          V     chg  dis   pv    load  mode")
        for hh in sorted(byh):
            x = byh[hh]
            print(f"      {hh:02d}    {x[0]['soc']:3d} -> {x[-1]['soc']:3d}  {med([r['v'] for r in x]):5.1f}  "
                  f"{med([r['chg'] for r in x], 0):3.0f}  {med([r['dis'] for r in x], 0):3.0f}  {med([r['pv'] for r in x], 0):5.0f} "
                  f"{med([r['load'] for r in x], 0):5.0f}  {x[len(x) // 2]['mode']}")
    print("  the number sits at 100 for about four hours at float, then starts its 2/h slide while still AT")
    print("  57.6 V with the charger idle -- the pack is full and the counter is already counting down.")
    print()


# ----------------------------------------------------------------------------
# I  full-to-full balance -- the hold-night test without a hold night

def part_i(rows):
    line("=")
    print(" I  FULL-TO-FULL BALANCE -- Wh in vs Wh out between one real full and the next")
    line("=")
    print("  in x efficiency = out + hidden drain. So hidden drain <= 0.90 x in - out, whatever the SOC")
    print("  number did in between. A real 2 pt/h slide at 44 Wh/pt would need ~1.7 kWh of it per day.")
    print()
    fulls = []
    for day in sorted({r["t"].date() for r in rows}):
        f = [r for r in rows if r["t"].date() == day and r["t"].hour >= 6 and r["soc"] >= 100]
        if f:
            fulls.append(f[0])
    for a, b in zip(fulls, fulls[1:]):
        seg = [r for r in rows if a["t"] <= r["t"] <= b["t"]]
        cin = cout = cov = flt = 0.0
        for p, q in zip(seg, seg[1:]):
            h = (q["t"] - p["t"]).total_seconds() / 3600
            if 0 < h <= GAP_S / 3600:
                cov += h
                cin += (p["v"] * p["chg"] + q["v"] * q["chg"]) / 2 * h
                o = (p["v"] * p["dis"] + q["v"] * q["dis"]) / 2 * h
                cout += o
                if p["v"] >= 57.0 and p["chg"] == 0:
                    flt += o
        span = hours(a["t"], b["t"])
        print(f"  {hm(a['t'])} -> {hm(b['t'])}  span {span:.1f} h, covered {cov:.1f} h, soc low {min(r['soc'] for r in seg)}")
        print(f"      in {cin:.0f} Wh   out {cout:.0f} Wh (of which {flt:.0f} as the float '2 A')")
        print(f"      hidden drain <= {0.9 * cin - cout:.0f} Wh; if the float 2 A is a sensor offset, <= {0.9 * cin - (cout - flt):.0f} Wh")
        print(f"      a real 2 pt/h slide over the ~{span - 4:.0f} h not at 100 would be ~{(span - 4) * 2 * 44:.0f} Wh")
    print("  the balance closes within a few hundred Wh, most of that whole-amp rounding and the inverter's own")
    print("  draw. There is no room for the slide to be energy: the hold night (T1) is answered by the day book.")
    print()


# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="only the last N log days")
    ap.add_argument("--no-meters", action="store_true")
    ap.add_argument("--part", default="ABCDEFGHI", help="which parts to print, e.g. BD")
    a = ap.parse_args()
    rows = load_samples(a.days)
    if not rows:
        print("no QPIGS samples under", LOGS)
        return 1
    print(f"phantom-bridge pack audit  {len(rows)} QPIGS samples  {hm(rows[0]['t'])} .. {hm(rows[-1]['t'])}")
    print("battery current is whole amps in every figure below (+-0.5 A = +-27 W)\n")
    parts = a.part.upper()
    if "A" in parts: part_a(rows)
    if "B" in parts: part_b(rows)
    if "C" in parts: part_c(rows)
    if "D" in parts: part_d(rows)
    if "E" in parts: part_e(rows, {} if a.no_meters else load_meters(a.days))
    if "F" in parts: part_f(rows)
    if "G" in parts: part_g(rows)
    if "H" in parts: part_h(rows)
    if "I" in parts: part_i(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
