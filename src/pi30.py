"""PI30 command catalogue and response decoders.

Every command this project can send lives here, reads and writes both, with
the field maps needed to turn a reply into named values.

Field orders come from the published Axpert/Voltronic PI30 protocol. They are
well attested but not verified against *this* unit, so every decoder is
tolerant: a short reply fills what it can and reports the rest as None rather
than raising. A field count that does not match the map is itself a finding --
`decode()` returns it as `_extra` / `_missing` so it lands in the log instead
of being silently dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# tolerant scalar conversion
# --------------------------------------------------------------------------

def _num(token, cast=float):
    try:
        return cast(token)
    except (TypeError, ValueError):
        return None


def _int(token):
    return _num(token, int)


def _flt(token):
    return _num(token, float)


def _str(token):
    return token


def _split(text: str) -> list[str]:
    return [tok for tok in text.strip().split(" ") if tok != ""]


# --------------------------------------------------------------------------
# field maps
# --------------------------------------------------------------------------

# QPIGS -- the live telemetry workhorse. 21 fields on a full implementation.
QPIGS_FIELDS = [
    ("grid_v",            _flt, "V"),
    ("grid_hz",           _flt, "Hz"),
    ("ac_out_v",          _flt, "V"),
    ("ac_out_hz",         _flt, "Hz"),
    ("ac_out_va",         _int, "VA"),
    ("ac_out_w",          _int, "W"),
    ("load_pct",          _int, "%"),
    ("bus_v",             _int, "V"),
    ("batt_v",            _flt, "V"),
    ("batt_charge_a",     _int, "A"),
    ("batt_soc",          _int, "%"),
    ("heatsink_c",        _int, "C"),
    ("pv_a",              _flt, "A"),
    ("pv_v",              _flt, "V"),
    ("batt_v_scc",        _flt, "V"),
    ("batt_discharge_a",  _int, "A"),
    ("status_bits",       _str, ""),
    ("batt_fan_offset",   _int, "10mV"),
    ("eeprom_version",    _int, ""),
    ("pv_charge_w",       _int, "W"),
    ("status_bits2",      _str, ""),
    # Fields 22-24 are not in the base 21-field spec but this unit sends them
    # (observed 2026-09-19: trailing "0 01 0000"). Named from the PI30 grid-feed
    # extension; the values have not been made to move yet, so treat the names
    # as probable rather than settled.
    ("solar_feed_to_grid", _int, ""),
    ("country_code",       _str, ""),
    ("solar_feed_w",       _int, "W"),
]

# QPIRI -- rated values. Settles the one-chassis-or-two question.
QPIRI_FIELDS = [
    ("grid_rating_v",        _flt, "V"),
    ("grid_rating_a",        _flt, "A"),
    ("ac_out_rating_v",      _flt, "V"),
    ("ac_out_rating_hz",     _flt, "Hz"),
    ("ac_out_rating_a",      _flt, "A"),
    ("ac_out_rating_va",     _int, "VA"),
    ("ac_out_rating_w",      _int, "W"),
    ("batt_rating_v",        _flt, "V"),
    ("batt_recharge_v",      _flt, "V"),
    ("batt_under_v",         _flt, "V"),
    ("batt_bulk_v",          _flt, "V"),
    ("batt_float_v",         _flt, "V"),
    ("batt_type",            _int, ""),
    ("max_utility_charge_a", _int, "A"),
    ("max_charge_a",         _int, "A"),
    ("input_voltage_range",  _int, ""),
    ("output_source_prio",   _int, ""),
    ("charger_source_prio",  _int, ""),
    ("parallel_max_units",   _int, ""),
    ("machine_type",         _str, ""),
    ("topology",             _int, ""),
    ("output_mode",          _int, ""),
    ("batt_redischarge_v",   _flt, "V"),
    ("pv_ok_condition",      _int, ""),
    ("pv_power_balance",     _int, ""),
]

# QPGSn -- per-unit status in a parallel stack.
QPGS_FIELDS = [
    ("parallel_exists",      _int, ""),
    ("serial",               _str, ""),
    ("work_mode",            _str, ""),
    ("fault_code",           _str, ""),
    ("grid_v",               _flt, "V"),
    ("grid_hz",              _flt, "Hz"),
    ("ac_out_v",             _flt, "V"),
    ("ac_out_hz",            _flt, "Hz"),
    ("ac_out_va",            _int, "VA"),
    ("ac_out_w",             _int, "W"),
    ("load_pct",             _int, "%"),
    ("batt_v",               _flt, "V"),
    ("batt_charge_a",        _int, "A"),
    ("batt_soc",             _int, "%"),
    ("pv_v",                 _flt, "V"),
    ("total_charge_a",       _int, "A"),
    ("total_ac_out_va",      _int, "VA"),
    ("total_ac_out_w",       _int, "W"),
    ("total_load_pct",       _int, "%"),
    ("inverter_status",      _str, ""),
    ("output_mode",          _int, ""),
    ("charger_source_prio",  _int, ""),
    ("max_charge_a",         _int, "A"),
    ("max_charge_range_a",   _int, "A"),
    ("max_utility_charge_a", _int, "A"),
    ("pv_a",                 _flt, "A"),
    ("batt_discharge_a",     _int, "A"),
]

MODE_NAMES = {
    "P": "power-on", "S": "standby", "L": "line", "B": "battery",
    "F": "fault", "H": "power-saving",
    # seen on some firmwares, not in the base spec
    "D": "shutdown", "Y": "bypass", "E": "eco",
}

BATTERY_TYPES = {0: "AGM", 1: "Flooded", 2: "User-defined",
                 3: "Li / Pylon (model dependent)"}
OUTPUT_PRIORITIES = {0: "Utility-Solar-Battery", 1: "Solar-Utility-Battery",
                     2: "Solar-Battery-Utility (SBU)"}
CHARGER_PRIORITIES = {0: "Utility first", 1: "Solar first",
                      2: "Solar + Utility", 3: "Solar only"}
INPUT_RANGES = {0: "Appliance", 1: "UPS"}
OUTPUT_MODES = {0: "single", 1: "parallel", 2: "Phase 1 of 3",
                3: "Phase 2 of 3", 4: "Phase 3 of 3"}

# QPIGS device status, first group. Index 0 is b7.
STATUS_BITS = [
    ("sbu_priority_version", "b7"),
    ("config_changed",       "b6"),
    ("scc_firmware_updated", "b5"),
    ("load_on",              "b4"),
    ("batt_v_steady",        "b3"),
    ("charging",             "b2"),
    ("charging_scc",         "b1"),
    ("charging_ac",          "b0"),
]

# QPIGS device status, second group. Index 0 is b10.
STATUS_BITS2 = [
    ("charging_to_float", "b10"),
    ("switch_on",         "b11"),
    ("dustproof_fitted",  "b12"),
]

# QPIWS warning bitfield, a0 first.
WARNING_BITS = [
    "reserved_0",               # a0 -- 1 here means fault, 0 means warning
    "inverter_fault",
    "bus_over",
    "bus_under",
    "bus_soft_fail",
    "line_fail",
    "opv_short",
    "inverter_voltage_low",
    "inverter_voltage_high",
    "over_temperature",
    "fan_locked",
    "battery_voltage_high",
    "battery_low_alarm",
    "reserved_13",
    "battery_under_shutdown",
    "battery_derating",
    "over_load",
    "eeprom_fault",
    "inverter_over_current",
    "inverter_soft_fail",
    "self_test_fail",
    "op_dc_voltage_over",
    "battery_open",
    "current_sensor_fail",
    "battery_short",
    "power_limit",
    "pv_voltage_high",
    "mppt_overload_fault",
    "mppt_overload_warning",
    "battery_too_low_to_charge",
    "reserved_30",
    "reserved_31",
]

FLAG_LETTERS = {
    "a": "buzzer",
    "b": "overload_bypass",
    "j": "power_saving",
    "k": "lcd_escape_to_default",
    "u": "overload_restart",
    "v": "over_temperature_restart",
    "x": "backlight",
    "y": "alarm_on_primary_source_interrupt",
    "z": "fault_code_record",
}


# --------------------------------------------------------------------------
# decoders
# --------------------------------------------------------------------------

def _decode_table(text: str, fields) -> dict:
    """Map space-separated tokens onto a field table, tolerantly."""
    tokens = _split(text)
    out: dict = {}
    for index, (name, cast, _unit) in enumerate(fields):
        out[name] = cast(tokens[index]) if index < len(tokens) else None
    if len(tokens) > len(fields):
        out["_extra"] = tokens[len(fields):]
    if len(tokens) < len(fields):
        out["_missing"] = len(fields) - len(tokens)
    out["_field_count"] = len(tokens)
    return out


def decode_bits(bitstring, table) -> dict:
    """Positional bit table -> named booleans."""
    out: dict = {}
    if not bitstring:
        return out
    for index, (name, _label) in enumerate(table):
        out[name] = bitstring[index] == "1" if index < len(bitstring) else None
    return out


def decode_qpigs(text: str) -> dict:
    """>>> decode_qpigs("235.9 50.4 235.9 50.4 0405 0405 006 420 53.30 010 075 031 0000 000.0 53.30 00000 00010000 00 00 00000 010")["ac_out_w"]
    405
    """
    data = _decode_table(text, QPIGS_FIELDS)
    data["status"] = decode_bits(data.get("status_bits"), STATUS_BITS)
    data["status"].update(decode_bits(data.get("status_bits2"), STATUS_BITS2))
    return data


def decode_qpiri(text: str) -> dict:
    data = _decode_table(text, QPIRI_FIELDS)
    batt_type = data.get("batt_type")
    # This unit reports type 9, which is outside the documented 0-3 range, so
    # unknown codes are surfaced rather than silently dropped as None.
    data["batt_type_name"] = (None if batt_type is None else
                              BATTERY_TYPES.get(batt_type, f"unmapped code {batt_type}"))
    data["output_source_prio_name"] = OUTPUT_PRIORITIES.get(data.get("output_source_prio"))
    data["charger_source_prio_name"] = CHARGER_PRIORITIES.get(data.get("charger_source_prio"))
    data["input_voltage_range_name"] = INPUT_RANGES.get(data.get("input_voltage_range"))
    data["output_mode_name"] = OUTPUT_MODES.get(data.get("output_mode"))
    return data


def decode_qpgs(text: str) -> dict:
    data = _decode_table(text, QPGS_FIELDS)
    data["work_mode_name"] = MODE_NAMES.get((data.get("work_mode") or "").upper())
    return data


def decode_qmod(text: str) -> dict:
    """>>> decode_qmod("B")
    {'mode': 'B', 'mode_name': 'battery'}
    """
    code = text.strip()[:1].upper()
    return {"mode": code, "mode_name": MODE_NAMES.get(code, "unknown")}


def decode_qpiws(text: str) -> dict:
    """>>> decode_qpiws("100000000000000000000000000000000000")["clear"]
    True
    >>> decode_qpiws("00000000000000001000000000000000")["active"]
    ['over_load']
    """
    bits = text.strip()
    active = [
        WARNING_BITS[index]
        for index, char in enumerate(bits)
        if char == "1" and index < len(WARNING_BITS)
        and not WARNING_BITS[index].startswith("reserved")
    ]
    # a0 is documented as "1 = fault, 0 = warning", but this unit reports it set
    # while running normally in Line mode with every other bit clear (observed
    # 2026-09-19). So a0 alone is not evidence of anything -- the named bits are
    # the truth, and a0 is reported raw rather than interpreted.
    return {
        "bits": bits,
        "a0_set": bits[:1] == "1",
        "is_fault": bool(active),
        "active": active,
        "clear": not active,
    }


def decode_qflag(text: str) -> dict:
    """`EakxyDbjuvz` -> which flags are on and which are off.

    >>> decode_qflag("EakxyDbjuvz")["enabled"]
    ['buzzer', 'lcd_escape_to_default', 'backlight', 'alarm_on_primary_source_interrupt']
    """
    body = text.strip()
    enabled: list[str] = []
    disabled: list[str] = []
    bucket = None
    for char in body:
        if char == "E":
            bucket = enabled
        elif char == "D":
            bucket = disabled
        elif bucket is not None:
            bucket.append(FLAG_LETTERS.get(char.lower(), "unknown:" + char))
    return {"enabled": enabled, "disabled": disabled, "raw": body}


def decode_time(text: str) -> dict:
    from clock import parse_inverter_time
    parsed = parse_inverter_time(text)
    return {"raw": text.strip(),
            "datetime": parsed.isoformat(sep=" ") if parsed else None}


def decode_currents(text: str) -> dict:
    """QMCHGCR / QMUCHGCR -- the charge currents this unit will accept."""
    return {"selectable_a": [_int(tok) for tok in _split(text)]}


def decode_raw(text: str) -> dict:
    return {"raw": text.strip(), "tokens": _split(text)}


# --------------------------------------------------------------------------
# read catalogue
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Read:
    command: str
    doc: str
    decoder: object = decode_raw
    core: bool = False        # polled every cycle
    certain: bool = True      # False = may well NAK on this firmware


READS: dict[str, Read] = {r.command: r for r in [
    Read("QPI",      "Protocol id -- confirms the PI30 family", decode_raw),
    Read("QID",      "Inverter serial number", decode_raw),
    Read("QSID",     "Serial number, extended form", decode_raw, certain=False),
    Read("QVFW",     "Main CPU firmware version", decode_raw),
    Read("QVFW2",    "Secondary CPU firmware version", decode_raw, certain=False),
    Read("QMOD",     "Work mode -- L line, B battery, F fault ...", decode_qmod, core=True),
    Read("QPIGS",    "Live telemetry -- the load-flow source", decode_qpigs, core=True),
    Read("QPIRI",    "Rated values -- settles chassis count and setpoints", decode_qpiri),
    Read("QPIWS",    "Warning and fault bitfield", decode_qpiws, core=True),
    Read("QFLAG",    "Which feature flags are enabled", decode_qflag),
    Read("QDI",      "Factory default settings", decode_raw),
    Read("QMCHGCR",  "Selectable max total charge currents", decode_currents),
    Read("QMUCHGCR", "Selectable max utility charge currents", decode_currents),
    Read("QOPM",     "Output mode", decode_raw, certain=False),
    Read("QBOOT",    "DSP bootloader status", decode_raw, certain=False),
    Read("QBEQI",    "Equalization status and settings", decode_raw, certain=False),
    Read("QT",       "Inverter clock -- the readback that proves DAT", decode_time, certain=False),
    Read("QPGS0",    "Parallel unit 0 status", decode_qpgs),
    Read("QPGS1",    "Parallel unit 1 status -- answers only on a twin", decode_qpgs, certain=False),
    Read("QPGS2",    "Parallel unit 2 status", decode_qpgs, certain=False),
    Read("QPGS3",    "Parallel unit 3 status", decode_qpgs, certain=False),
    Read("QET",      "Total generated energy, Wh", decode_raw, certain=False),
    Read("QLT",      "Total load energy, Wh", decode_raw, certain=False),
]}


def decode(command: str, text: str) -> dict:
    """Decode a reply for `command`. Unknown commands come back as raw tokens.

    >>> decode("QMOD", "L")["mode_name"]
    'line'
    """
    base = command.upper()
    spec = READS.get(base)
    if spec is None and base.startswith("QPGS"):
        spec = READS.get("QPGS0")
    if spec is None:
        return decode_raw(text)
    decoder = spec.decoder
    return decoder(text) if callable(decoder) else decode_raw(text)


# --------------------------------------------------------------------------
# write catalogue -- every setter the protocol defines
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Setter:
    key: str                       # CLI name
    prefix: str                    # PI30 prefix
    kind: str                      # choice | volts | amps | flag | bare | datetime
    doc: str
    choices: dict = field(default_factory=dict)
    lo: float | None = None
    hi: float | None = None
    decimals: int = 1
    digits: int = 2                # integer digits for amps / volts formatting
    risk: str = ""                 # "" | parallel | high
    unit_index: bool = False       # accepts a parallel unit index prefix

    def render(self, value=None, unit: int | None = None) -> str:
        """Build the PI30 command string. Raises ValueError on bad input."""
        prefix = self.prefix
        if self.unit_index:
            prefix += str(0 if unit is None else unit)

        if self.kind == "bare":
            return prefix

        if self.kind == "choice":
            key = str(value).lower()
            if key not in self.choices:
                raise ValueError(
                    self.key + ": expected one of " + ", ".join(self.choices)
                    + ", got " + repr(value))
            return prefix + self.choices[key]

        if self.kind == "flag":
            key = str(value).lower()
            letter = next((ltr for ltr, name in FLAG_LETTERS.items()
                           if name == key or ltr == key), None)
            if letter is None:
                raise ValueError(
                    self.key + ": expected one of "
                    + ", ".join(sorted(FLAG_LETTERS.values())))
            return prefix + letter

        if self.kind == "datetime":
            return prefix + str(value)

        number = float(value)
        if self.lo is not None and number < self.lo:
            raise ValueError(self.key + ": " + str(number)
                             + " is below the " + str(self.lo) + " floor")
        if self.hi is not None and number > self.hi:
            raise ValueError(self.key + ": " + str(number)
                             + " is above the " + str(self.hi) + " ceiling")
        if self.kind == "volts":
            width = self.digits + 1 + self.decimals
            return prefix + "{0:0{1}.{2}f}".format(number, width, self.decimals)
        return prefix + "{0:0{1}d}".format(int(round(number)), self.digits)


# Voltage bounds below are for a 48 V bus, which this site has. They are guard
# rails against a typo, not the inverter's own limits -- it will NAK anything
# it dislikes regardless.
SETTERS: dict[str, Setter] = {s.key: s for s in [
    Setter("output-priority", "POP", "choice",
           "Output source priority",
           choices={"utility": "00", "solar": "01", "sbu": "02"},
           risk="parallel"),
    Setter("charger-priority", "PCP", "choice",
           "Charger source priority",
           choices={"utility": "00", "solar": "01",
                    "solar-and-utility": "02", "solar-only": "03"},
           risk="parallel"),
    Setter("output-freq", "F", "choice",
           "AC output frequency",
           choices={"50": "50", "60": "60"}, risk="parallel"),
    Setter("grid-range", "PGR", "choice",
           "Input voltage range -- UPS is tighter, Appliance is tolerant",
           choices={"appliance": "00", "ups": "01"}),
    Setter("battery-type", "PBT", "choice",
           "Battery type",
           choices={"agm": "00", "flooded": "01", "user": "02"},
           risk="parallel"),
    Setter("recharge-voltage", "PBCV", "volts",
           "Battery voltage at which utility takes back over",
           lo=40.0, hi=52.0, risk="parallel"),
    Setter("redischarge-voltage", "PBDV", "volts",
           "Battery voltage at which we return to battery (00.0 = when full)",
           lo=0.0, hi=62.0, risk="parallel"),
    Setter("cutoff-voltage", "PSDV", "volts",
           "Low-voltage shutdown",
           lo=40.0, hi=52.0, risk="parallel"),
    Setter("bulk-voltage", "PCVV", "volts",
           "Bulk / constant-voltage charge target",
           lo=48.0, hi=60.0, risk="parallel"),
    Setter("float-voltage", "PBFT", "volts",
           "Float charge voltage",
           lo=48.0, hi=60.0, risk="parallel"),
    Setter("max-charge-current", "MNCHGC", "amps",
           "Max total charge current, per unit index",
           lo=0, hi=200, digits=3, unit_index=True, risk="parallel"),
    Setter("max-utility-charge-current", "MUCHGC", "amps",
           "Max charge current drawn from utility, per unit index",
           lo=0, hi=100, digits=2, unit_index=True, risk="parallel"),
    Setter("enable", "PE", "flag",
           "Turn a feature flag on (QFLAG lists the names)"),
    Setter("disable", "PD", "flag",
           "Turn a feature flag off (QFLAG lists the names)"),
    Setter("output-mode", "POPM", "choice",
           "Output mode -- single, parallel, or one phase of three",
           choices={"single": "00", "parallel": "01", "phase1": "02",
                    "phase2": "03", "phase3": "04"},
           unit_index=True, risk="parallel"),
    Setter("pv-ok-condition", "PPVOKC", "choice",
           "Whether one unit's PV being OK is enough, or all must be",
           choices={"any": "0", "all": "1"}, risk="parallel"),
    Setter("solar-power-balance", "PSPB", "choice",
           "Limit PV input to charge current, or allow load + charge",
           choices={"charge-only": "0", "load-and-charge": "1"}, risk="parallel"),
    Setter("equalize", "PBEQE", "choice",
           "Battery equalization on or off",
           choices={"on": "1", "off": "0"}, risk="parallel"),
    Setter("equalize-now", "PBEQA", "choice",
           "Start or cancel equalization immediately",
           choices={"start": "1", "cancel": "0"}, risk="parallel"),
    Setter("equalize-time", "PBEQT", "amps",
           "Equalization duration, minutes",
           lo=5, hi=900, digits=3, risk="parallel"),
    Setter("equalize-period", "PBEQP", "amps",
           "Days between equalization cycles",
           lo=0, hi=90, digits=3, risk="parallel"),
    Setter("equalize-voltage", "PBEQV", "volts",
           "Equalization voltage",
           lo=48.0, hi=62.0, decimals=2, risk="parallel"),
    Setter("equalize-timeout", "PBEQOT", "amps",
           "Equalization timeout, minutes",
           lo=5, hi=900, digits=3, risk="parallel"),
    Setter("datetime", "DAT", "datetime",
           "Set the inverter RTC -- YYMMDDHHMMSS. Not in the published spec."),
    Setter("factory-reset", "PF", "bare",
           "Restore factory defaults. Wipes every setting above.",
           risk="high"),
]}


def build_write(key: str, value=None, unit: int | None = None) -> str:
    """Friendly name + value -> PI30 command string.

    >>> build_write("output-priority", "sbu")
    'POP02'
    >>> build_write("float-voltage", 54.0)
    'PBFT54.0'
    >>> build_write("max-charge-current", 60, unit=0)
    'MNCHGC0060'
    >>> build_write("equalize-voltage", 58.4)
    'PBEQV58.40'
    >>> build_write("enable", "buzzer")
    'PEa'
    >>> build_write("factory-reset")
    'PF'
    """
    setter = SETTERS.get(key)
    if setter is None:
        raise KeyError("no such control: " + key)
    return setter.render(value, unit)


if __name__ == "__main__":
    import doctest
    print(doctest.testmod())
