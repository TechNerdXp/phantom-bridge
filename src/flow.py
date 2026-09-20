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

QMOD's `B` does NOT mean "running on the battery". Audited 2026-09-20 against
266 daytime samples: the unit sat in B all morning with 900 W of sun, the
pack charging at 7 A and the grid present at 232 V. On this family `B` is
*inverter* mode -- the output is synthesised from the DC bus, which the PV
and the battery both feed -- and `L` is *line* mode, the load bypassed
straight from the grid. With output priority SBU the unit lives in B whenever
the sun or the pack can carry the house. So the mode says which stage makes
the AC; the currents say who is paying for it. `source` below is the answer
to the second question, and it is what a screen should show.
"""
from __future__ import annotations

import datetime as dt

# A grid leg below this is rounding noise, not a real import.
GRID_NOISE_W = 15
# PV below this is "the sun is not up" for the source verdict.
PV_LIVE_W = 20

# What QMOD's letter means for the output stage. The vendor calls B "battery
# mode" and that name is the whole confusion: it is inverter output.
MODE_WORDS = {"L": "grid bypass", "B": "inverter", "F": "fault",
              "P": "power on", "S": "standby", "H": "power saving",
              "D": "shutdown", "Y": "bypass", "E": "eco"}


def source_of(mode_code: str, pv_w: float, discharge_a: float,
              grid_present: bool) -> tuple[str, str]:
    """Who is carrying the house: a code and a label for the top of a screen.

    >>> source_of("B", 948, 0, True)
    ('solar', 'ON SOLAR')
    >>> source_of("B", 120, 9, True)
    ('solar+battery', 'SOLAR + BATTERY')
    >>> source_of("B", 0, 9, True)
    ('battery', 'ON BATTERY')
    >>> source_of("B", 0, 9, False)
    ('battery', 'GRID OUT')
    >>> source_of("L", 300, 0, True)
    ('grid', 'ON GRID')
    >>> source_of("F", 0, 0, True)
    ('fault', 'FAULT')
    """
    if mode_code == "B":
        sunny = pv_w > PV_LIVE_W
        if discharge_a > 0 and sunny:
            return "solar+battery", "SOLAR + BATTERY"
        if discharge_a > 0 or not sunny:
            return "battery", "ON BATTERY" if grid_present else "GRID OUT"
        return "solar", "ON SOLAR"
    if mode_code == "L":
        return "grid", "ON GRID"
    if mode_code == "F":
        return "fault", "FAULT"
    if mode_code in MODE_WORDS:
        return mode_code.lower(), MODE_WORDS[mode_code].upper()
    # No mode letter: judge from the currents alone.
    if discharge_a > 0:
        return "battery", "ON BATTERY" if grid_present else "GRID OUT"
    if grid_present:
        return "grid", "ON GRID"
    return "unknown", "NO DATA"


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
    out_v = _f(qpigs.get("ac_out_v"))

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
    grid_present = grid_v >= grid_present_volts
    on_battery = mode_code == "B" and discharge_a > 0
    # A disagreement is the inverter stage running with nothing feeding it
    # (B, dark, no discharge -- a rounding or a transfer) or the grid bypass
    # engaged while the pack is being drawn (L with discharge). B with sun and
    # no discharge is not one: that is the unit doing its job on solar.
    disagreement = ((mode_code == "B" and discharge_a <= 0 and pv_w <= PV_LIVE_W)
                    or (mode_code == "L" and discharge_a > 0))
    source, source_label = source_of(mode_code, pv_w, discharge_a, grid_present)

    result = {
        "mode": mode_code or None,
        "mode_word": MODE_WORDS.get(mode_code),
        "source": source,
        "source_label": source_label,
        "on_battery": on_battery,
        "on_battery_disagreement": disagreement,
        "grid_present": grid_present,
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
        # Output current is not a PI30 field either: VA / V, which is what a
        # clamp on the output would read. It is what the cable rating is in.
        "load_a": round(load_va / out_v, 1) if out_v else None,
        "power_factor": round(load_w / load_va, 2) if load_va else None,
        # The secondary numbers, carried through so a display can show them
        # small without going back to the raw QPIGS record.
        "grid_v": grid_v,
        "grid_hz": qpigs.get("grid_hz"),
        "out_v": out_v,
        "out_hz": qpigs.get("ac_out_hz"),
        "pv_v": qpigs.get("pv_v"),
        "pv_a": qpigs.get("pv_a"),
        "batt_soc": qpigs.get("batt_soc"),
        "soc_trusted": trust_soc,
        "heatsink_c": qpigs.get("heatsink_c"),
        "bus_v": qpigs.get("bus_v"),
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


def leg_stress(flow: dict, cfg) -> dict:
    """How close each leg is to its own limit: {leg: (fraction, warn_at)}.

    Grid and home are amps against the 7/29 (`cfg.GRID_LINE_RATING_A`,
    `cfg.LOAD_LINE_RATING_A`); the home leg is both output lines summed
    against ONE line's rating, because the split is unknown. Battery is amps
    against `cfg.BATTERY_MAX_A`, amber from `cfg.BATTERY_CONTINUOUS_A`. A
    leg with no limit configured, or nothing flowing, is None. Shared by
    the watch screen and the tray so the two never disagree.

    >>> import types
    >>> C = types.SimpleNamespace(GRID_LINE_RATING_A=25.0, LOAD_LINE_RATING_A=25.0,
    ...                           LOAD_LINE_WARN_FRACTION=0.8, BATTERY_MAX_A=100.0,
    ...                           BATTERY_CONTINUOUS_A=50.0)
    >>> st = leg_stress({"grid_w": 2400.0, "grid_v": 240.0, "load_a": 30.0,
    ...                  "batt_discharge_a": 60.0, "batt_charge_a": 0.0}, C)
    >>> round(st["grid"][0], 2), round(st["home"][0], 2), st["battery"]
    (0.4, 1.2, (0.6, 0.5))
    >>> [leg for leg, v in st.items() if v and v[0] >= 1.0]
    ['home']
    """
    grid_w = flow.get("grid_w") or 0.0
    grid_v = flow.get("grid_v") or 0.0
    grid_a = grid_w / grid_v if grid_v and grid_w > 1 else 0.0
    load_a = flow.get("load_a") or 0.0
    batt_a = max(flow.get("batt_discharge_a") or 0.0, flow.get("batt_charge_a") or 0.0)
    out = {}
    warn = getattr(cfg, "LOAD_LINE_WARN_FRACTION", 0.8)
    rating = getattr(cfg, "GRID_LINE_RATING_A", 0) or 0
    out["grid"] = (grid_a / rating, warn) if rating else None
    rating = getattr(cfg, "LOAD_LINE_RATING_A", 0) or 0
    out["home"] = (load_a / rating, warn) if rating else None
    top = getattr(cfg, "BATTERY_MAX_A", 0) or 0
    cont = getattr(cfg, "BATTERY_CONTINUOUS_A", 0) or 0
    out["battery"] = (batt_a / top, (cont / top) if cont else 0.8) if top else None
    return out


def at_limit(stress: dict) -> list[str]:
    """The legs at or past their limit, in display order."""
    return [leg for leg in ("grid", "home", "battery")
            if stress.get(leg) and stress[leg][0] >= 1.0]


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

def _bar(value: float, full: float, width: int, grow_left: bool = False) -> str:
    """One bar cell. Filled from the centre axis outward."""
    if full <= 0:
        return "." * width
    n = max(0, min(width, int(round(width * value / full))))
    if value > 0 and n == 0:
        n = 1                      # a live source should never read as nothing
    return ("." * (width - n) + "#" * n) if grow_left else ("#" * n + "." * (width - n))


def _watts(value) -> str:
    if value is None:
        return "--"
    if abs(value) >= 10000:
        return f"{value / 1000:.1f}k"
    return f"{value:.0f}"


NAME_W, VAL_W, BAR_W = 14, 7, 15


def render(flow: dict, *, when: dt.datetime | None = None,
           warnings: dict | None = None, width: int = 76) -> str:
    """Every source and every load at once, mirrored about a centre axis.

    The vendor app shows one thing at a time and leads with volts and hertz.
    This leads with watts and puts both halves of the energy balance on one
    screen, drawn to ONE shared scale -- so a bar on the left means the same
    number of watts as a bar on the right. That shared scale is the whole
    point; per-row scaling would make a 20 W trickle look like a 3 kW load.

    ASCII only: the Windows console code page will not render box drawing.
    """
    charge = max(0.0, flow.get("batt_w") or 0.0)
    discharge = max(0.0, -(flow.get("batt_w") or 0.0))
    pv = flow.get("pv_w") or 0.0
    grid = flow.get("grid_w") or 0.0
    load = flow.get("load_w") or 0.0

    sources = [
        ("Solar", pv, "" if flow.get("pv_measured", True) else "V*A"),
        ("Grid", grid, "derived"),
        ("Battery out", discharge,
         f"{flow.get('batt_discharge_a', 0):.0f} A" if discharge else ""),
    ]
    loads = [
        ("Household", load,
         f"{flow['load_pct']}%" if flow.get("load_pct") is not None else ""),
        ("Battery in", charge,
         f"{flow.get('batt_charge_a', 0):.0f} A" if charge else ""),
    ]
    scale = max(pv, grid, discharge, load, charge, 1.0)

    mode = flow.get("mode") or "?"
    verdict = flow.get("source_label") or (
        "ON BATTERY" if flow.get("on_battery") else
        "on grid" if flow.get("grid_present") else "no grid")
    stamp = (when or dt.datetime.now()).strftime("%H:%M:%S")

    half = NAME_W + 1 + VAL_W + 2 + BAR_W
    total = half * 2 + 1
    rule = "-" * total
    out = [rule,
           f" Phantom II   mode {mode}   {verdict}".ljust(total - len(stamp) - 1)
           + stamp + " ",
           rule,
           "SOURCES".rjust(half) + " " + "LOADS".ljust(half)]

    for index in range(max(len(sources), len(loads))):
        line = ""
        if index < len(sources):
            name, watts, note = sources[index]
            line += (f"{name:>{NAME_W}} {_watts(watts):>{VAL_W}} W "
                     + _bar(watts, scale, BAR_W, grow_left=True))
        else:
            line += " " * half
        line += "|"
        if index < len(loads):
            name, watts, note = loads[index]
            line += (_bar(watts, scale, BAR_W)
                     + f" {_watts(watts):>{VAL_W}} W {name:<{NAME_W}}")
        out.append(line.rstrip())

    # The secondary numbers, on one line, deliberately not competing above.
    notes = []
    soc = flow.get("batt_soc")
    batt = f"Battery {flow.get('batt_v', 0):.2f} V"
    if soc is not None:
        batt += f"  {soc}%" + ("" if flow.get("soc_trusted") else " est")
    state = ("discharging" if discharge else "charging" if charge else "idle")
    batt += f"  {state}"
    if flow.get("runtime_h"):
        batt += f"  ~{flow['runtime_h']:.1f} h left"
    out += [rule, " " + batt]

    if flow.get("power_factor"):
        notes.append(f"pf {flow['power_factor']:.2f}")
    if flow.get("load_va"):
        notes.append(f"{flow['load_va']:.0f} VA")
    if notes:
        out.append(" " + "   ".join(notes))

    alerts = []
    if flow.get("on_battery"):
        alerts.append(f"ON BATTERY: drawing {_watts(load)} W from the pack")
    if flow.get("on_battery_disagreement"):
        alerts.append("mode and battery current disagree -- transitional")
    if warnings and warnings.get("active"):
        alerts.append("WARNING: " + ", ".join(warnings["active"]))
    if flow.get("chassis_hint"):
        alerts.append(flow["chassis_hint"])
    for alert in alerts:
        for chunk in _wrap(alert, total - 2):
            out.append(" " + chunk)
    out.append(rule)
    return "\n".join(out)


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
