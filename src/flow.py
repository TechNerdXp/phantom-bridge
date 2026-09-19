"""Where the power is actually coming from, and whether we are on battery.

QPIGS measures PV, battery and load directly. It does *not* measure grid
power -- there is no grid-current field in the protocol -- so the grid leg is
derived from the energy balance:

    pv + grid + discharge  =  load + charge + losses

giving  grid = load + charge - pv - discharge, floored at zero. It is marked
`derived` in the output so nobody mistakes it for a measurement.

The on-battery verdict is deliberately conservative, per the project's first
goal: QMOD == B *and* discharge current > 0. Mode alone flips during transfer
glitches and brownouts; current alone can be a brief transient. Both together
is the thing worth alerting on.
"""
from __future__ import annotations

import datetime as dt

# A grid leg below this is rounding noise, not a real import.
GRID_NOISE_W = 15


def _f(value, default=0.0) -> float:
    return default if value is None else float(value)


def analyse(qpigs: dict, mode: str | None = None, qpiri: dict | None = None,
            *, trust_soc: bool = False, capacity_ah: float | None = None,
            usable_fraction: float = 0.8,
            grid_present_volts: float = 80.0) -> dict:
    """Turn a QPIGS dict (+ optional QMOD/QPIRI) into a power-flow picture."""
    batt_v = _f(qpigs.get("batt_v"))
    charge_a = _f(qpigs.get("batt_charge_a"))
    discharge_a = _f(qpigs.get("batt_discharge_a"))
    load_w = _f(qpigs.get("ac_out_w"))
    load_va = _f(qpigs.get("ac_out_va"))
    grid_v = _f(qpigs.get("grid_v"))

    pv_w = qpigs.get("pv_charge_w")
    pv_measured = pv_w is not None
    if not pv_measured:
        pv_w = _f(qpigs.get("pv_v")) * _f(qpigs.get("pv_a"))
    pv_w = _f(pv_w)

    charge_w = batt_v * charge_a
    discharge_w = batt_v * discharge_a
    batt_w = charge_w - discharge_w          # positive = charging

    grid_w = load_w + charge_w - pv_w - discharge_w
    if grid_w < GRID_NOISE_W:
        grid_w = 0.0

    mode_code = (mode or "").strip()[:1].upper()
    on_battery = mode_code == "B" and discharge_a > 0
    mode_says_battery = mode_code == "B"
    current_says_battery = discharge_a > 0

    result = {
        "mode": mode_code or None,
        "on_battery": on_battery,
        "on_battery_disagreement": mode_says_battery != current_says_battery,
        "grid_present": grid_v >= grid_present_volts,
        "pv_w": round(pv_w, 1),
        "pv_measured": pv_measured,
        "grid_w": round(grid_w, 1),
        "grid_derived": True,
        "batt_w": round(batt_w, 1),
        "batt_charge_w": round(charge_w, 1),
        "batt_discharge_w": round(discharge_w, 1),
        "batt_v": batt_v,
        "batt_charge_a": charge_a,
        "batt_discharge_a": discharge_a,
        "load_w": round(load_w, 1),
        "load_va": round(load_va, 1),
        "load_pct": qpigs.get("load_pct"),
        "power_factor": round(load_w / load_va, 2) if load_va else None,
        "runtime_h": None,
        "runtime_basis": "SOC not trusted -- see config.TRUST_SOC",
    }

    if trust_soc and capacity_ah and discharge_a > 0:
        soc = qpigs.get("batt_soc")
        if soc is not None:
            usable_ah = capacity_ah * usable_fraction * (soc / 100.0)
            result["runtime_h"] = round(usable_ah / discharge_a, 2)
            result["runtime_basis"] = (
                f"{capacity_ah:.0f} Ah x {usable_fraction:.0%} usable x {soc}% SOC"
                f" / {discharge_a:.0f} A")
    elif trust_soc and not capacity_ah:
        result["runtime_basis"] = "set config.BATTERY_CAPACITY_AH to estimate runtime"

    result.update(_rating_check(qpigs, qpiri))
    return result


def _rating_check(qpigs: dict, qpiri: dict | None) -> dict:
    """Does the load percentage imply one chassis or two?

    The app showed 405 W at 6%, which implies the percentage is sized against
    a ~6.75 kVA machine -- one unit, not a 12 kVA pair. This recomputes that
    inference from live data and compares it with the unit's own rating.
    """
    load_w = _f(qpigs.get("ac_out_w"))
    load_pct = qpigs.get("load_pct")
    out: dict = {"implied_rating_va": None, "rated_va": None, "chassis_hint": None}
    if qpiri:
        out["rated_va"] = qpiri.get("ac_out_rating_va")
    if not load_pct or load_w <= 0:
        return out
    implied = round(load_w / (load_pct / 100.0))
    out["implied_rating_va"] = implied
    rated = out["rated_va"]
    if rated:
        ratio = implied / rated
        if ratio > 1.6:
            out["chassis_hint"] = (
                f"load% is sized against ~{implied} VA but this unit rates "
                f"{rated} VA -- consistent with {round(ratio)} units in parallel")
        else:
            out["chassis_hint"] = (
                f"load% matches this unit's own {rated} VA rating -- single "
                f"chassis, or the app is showing the master only")
    return out


# --------------------------------------------------------------------------
# rendering -- ASCII only, the Windows console code page is not to be trusted
# --------------------------------------------------------------------------

def _bar(value: float, full: float, width: int = 20) -> str:
    if full <= 0:
        return " " * width
    filled = max(0, min(width, int(round(width * value / full))))
    return "#" * filled + "." * (width - filled)


def render(flow: dict, *, when: dt.datetime | None = None,
           warnings: dict | None = None, width: int = 62) -> str:
    """A one-screen power-flow panel."""
    stamp = (when or dt.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    mode = flow.get("mode") or "?"
    verdict = "ON BATTERY" if flow["on_battery"] else (
        "on grid" if flow["grid_present"] else "no grid")

    peak = max(flow["pv_w"], flow["grid_w"], abs(flow["batt_w"]),
               flow["load_w"], 1.0)

    lines = []
    lines.append("+" + "-" * (width - 2) + "+")
    head = f" {stamp}   mode {mode}   {verdict} "
    lines.append("|" + head.ljust(width - 2) + "|")
    lines.append("+" + "-" * (width - 2) + "+")

    def row(label: str, watts: float, detail: str) -> None:
        body = f" {label:<6}{watts:>7.0f} W  {_bar(abs(watts), peak, 16)}  {detail}"
        lines.append("|" + body[:width - 2].ljust(width - 2) + "|")

    pv_note = "" if flow["pv_measured"] else " (V*A)"
    row("PV", flow["pv_w"], f"{pv_note}".strip())
    row("GRID", flow["grid_w"], "derived")
    batt_detail = ("charging" if flow["batt_w"] > 0 else
                   "DISCHARGING" if flow["batt_w"] < 0 else "idle")
    row("BATT", flow["batt_w"], f"{flow['batt_v']:.2f} V  {batt_detail}")
    load_detail = f"{flow['load_pct']}%" if flow["load_pct"] is not None else ""
    if flow.get("power_factor"):
        load_detail += f"  pf {flow['power_factor']:.2f}"
    row("LOAD", flow["load_w"], load_detail)

    lines.append("+" + "-" * (width - 2) + "+")

    notes = []
    if flow["on_battery"]:
        detail = f"discharging {flow['batt_discharge_a']:.0f} A"
        if flow.get("runtime_h"):
            detail += f", ~{flow['runtime_h']:.1f} h left"
        notes.append("ON BATTERY: " + detail)
    if flow["on_battery_disagreement"]:
        notes.append("DISAGREEMENT: mode and discharge current do not agree "
                     "-- treat as transitional")
    if warnings and warnings.get("active"):
        notes.append("WARNINGS: " + ", ".join(warnings["active"]))
    if flow.get("chassis_hint"):
        notes.append(flow["chassis_hint"])
    for note in notes:
        for chunk in _wrap(note, width - 4):
            lines.append("| " + chunk.ljust(width - 4) + " |")
    if notes:
        lines.append("+" + "-" * (width - 2) + "+")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, out, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out
