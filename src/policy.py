"""Output-priority policy: SBU by day, SUB from dusk, SBU again once the
pack can carry the rest of the night, and SUB at the floor.

The owner's rule (2026-09-22), which this replaces the inverter's own timer
(menu 99) with:

  * When the sun goes down -- the data says when, not the clock -- switch
    to SUB. The house runs on the grid and the pack is kept as the reserve
    an outage will need. If the grid drops, the inverter uses the pack
    regardless; the setting only says who carries the house while the
    grid is there.
  * Later in the night, work out how long what is left above the floor
    would carry the load at the hours ahead, and release the pack (SBU) at
    the moment that drains it to the floor just as the sun starts lifting
    it -- yesterday's lowest-SOC time. Eleven, two, three: whatever the
    load makes it, recomputed every cycle, so an outage that spent some of
    the reserve pushes the release later on its own.
  * At the floor, at any hour, SUB: the grid carries the house and the
    pack sits until the turnaround, and then until the sun has lifted it
    back over the resume level. Then SBU, the day's setting.

Pure: no link, no files, no clock of its own. build_profile() turns day
analyses (insights.analyse_day, via days.DayStore) into a Profile; a
Governor turns a Profile, the time and the SOC into a Decision. Doctested.
Time-of-day values are minutes after midnight, site time.
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field

UTI, SUB, SBU = 0, 1, 2                # QPIRI output_source_prio codes
NAMES = {UTI: "UTI", SUB: "SUB", SBU: "SBU"}
POP_CHOICE = {SUB: "solar", SBU: "sbu"}   # pi30.build_write("output-priority", .)

# A day's lowest SOC that falls in here is the dawn turnaround: the moment
# the sun started lifting the pack. Outside it the low is something else
# (an evening outage, a pack drained at midnight) and is not used.
MORNING = (3 * 60, 12 * 60)

# The release time is searched in steps of this many minutes.
STEP_MIN = 5

# Nominal 48 V LiFePO4 bus, for BATTERY_CAPACITY_AH -> Wh.
NOMINAL_V = 51.2


def parse_hm(text) -> int | None:
    """'07:14' -> 434. None (or junk) -> None.

    >>> parse_hm("07:14"), parse_hm(None), parse_hm("x")
    (434, None, None)
    """
    if not text or not isinstance(text, str) or ":" not in text:
        return None
    try:
        h, m = text.split(":")[:2]
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def fmt_hm(minutes) -> str:
    """434 -> '07:14'.

    >>> fmt_hm(434), fmt_hm(24 * 60 + 5)
    ('07:14', '00:05')
    """
    minutes = int(round(minutes)) % (24 * 60)
    return "%02d:%02d" % divmod(minutes, 60)


def _minutes(when: dt.datetime) -> float:
    return when.hour * 60 + when.minute + when.second / 60.0


# --------------------------------------------------------------------------
# the profile -- what the last days say the nights look like
# --------------------------------------------------------------------------

@dataclass
class Profile:
    turnaround_min: int              # the sun starts lifting the pack
    dusk_min: int                    # the sun stops carrying the house
    pack_wh: float                   # what 100 % of the SOC is worth
    efficiency: float                # load Wh per battery Wh
    hourly_load_w: list              # 24 entries, watts
    days: int = 0                    # finished days it was read from
    notes: dict = field(default_factory=dict)   # where each figure came from
    built_for: dt.date | None = None            # the day it was built on

    def as_dict(self) -> dict:
        return {"turnaround": fmt_hm(self.turnaround_min),
                "dusk": fmt_hm(self.dusk_min),
                "pack_wh": round(self.pack_wh),
                "efficiency": round(self.efficiency, 3),
                "hourly_load_w": [round(w) for w in self.hourly_load_w],
                "days": self.days, "notes": dict(self.notes),
                "built_for": self.built_for.isoformat() if self.built_for else None}


def build_profile(days: list, *, default_turnaround: str = "07:00",
                  default_dusk: str = "17:00", default_load_w: float = 400.0,
                  dusk_fraction: float = 0.25, pack_wh: float | None = None,
                  capacity_ah: float | None = None,
                  fallback_pack_wh: float = 2000.0,
                  fallback_efficiency: float = 0.88) -> Profile:
    """Read the figures out of day analyses, most recent first.

    Each entry is what insights.analyse_day returns for one finished day.
    Yesterday alone gives the turnaround and the dusk ("just like
    yesterday"); the whole list gives the hourly load, the pack size the
    episodes imply and the battery-to-load efficiency. Anything missing
    falls back to the defaults, and `notes` says which.

    >>> prof = build_profile([])
    >>> prof.as_dict()["turnaround"], prof.as_dict()["dusk"], prof.pack_wh
    ('07:00', '17:00', 2000.0)
    >>> day = _fake_day("2026-09-21", soc_min_at="07:14", pv_last="17:26")
    >>> prof = build_profile([day])
    >>> fmt_hm(prof.turnaround_min), fmt_hm(prof.dusk_min), prof.days
    ('07:14', '17:00', 1)
    >>> prof.pack_wh, round(prof.efficiency, 2)
    (4800.0, 0.9)
    >>> [prof.hourly_load_w[h] for h in (0, 3, 12, 20)]
    [300.0, 100.0, 1000.0, 500.0]
    """
    notes: dict = {}
    days = [d for d in (days or []) if d and d.get("samples")]

    # -- the turnaround: yesterday's lowest SOC, if it was a dawn low
    turnaround = None
    for d in days:
        m = parse_hm(d.get("soc_min_at"))
        if m is not None and MORNING[0] <= m < MORNING[1]:
            turnaround = m
            notes["turnaround"] = "lowest SOC %s at %s" % (d.get("date"), d.get("soc_min_at"))
            break
    if turnaround is None:
        for d in days:
            m = parse_hm(d.get("pv_first"))
            if m is not None:
                turnaround = m
                notes["turnaround"] = "first sun %s at %s" % (d.get("date"), d.get("pv_first"))
                break
    if turnaround is None:
        turnaround = parse_hm(default_turnaround) or 7 * 60
        notes["turnaround"] = "default"

    # -- dusk: the end of the last hour still at a quarter of the best hour
    dusk = None
    for d in days:
        hours = [(h.get("hour"), h.get("pv_w")) for h in (d.get("hours") or [])
                 if h.get("pv_w") is not None and h.get("hour") is not None]
        if not hours:
            continue
        peak = max(w for _, w in hours)
        if peak < 100:
            continue                      # no real sun in that record
        last = max(h for h, w in hours if w >= dusk_fraction * peak)
        dusk = (last + 1) * 60
        pv_last = parse_hm(d.get("pv_last"))
        if pv_last is not None:
            dusk = min(dusk, pv_last)
        notes["dusk"] = "PV under %d%% of its best hour after %s on %s" % (
            round(dusk_fraction * 100), fmt_hm(dusk), d.get("date"))
        break
    if dusk is None:
        dusk = parse_hm(default_dusk) or 17 * 60
        notes["dusk"] = "default"
    if dusk <= turnaround:
        turnaround = parse_hm(default_turnaround) or 7 * 60
        dusk = parse_hm(default_dusk) or 17 * 60
        notes["turnaround"] = notes["dusk"] = "default (the data put dusk before dawn)"

    # -- the pack, in SOC-scale watt-hours
    if pack_wh:
        pack, notes["pack_wh"] = float(pack_wh), "config BATTERY_PACK_WH"
    else:
        implied = [e["implied_pack_wh"] for d in days for e in (d.get("episodes") or [])
                   if e.get("implied_pack_wh")]
        if implied:
            pack = float(statistics.median(implied))
            notes["pack_wh"] = "median of %d episodes" % len(implied)
        elif capacity_ah:
            pack, notes["pack_wh"] = float(capacity_ah) * NOMINAL_V, "BATTERY_CAPACITY_AH"
        else:
            pack, notes["pack_wh"] = float(fallback_pack_wh), "fallback"

    # -- efficiency: load Wh per battery Wh on night episodes
    batt = load = 0.0
    for d in days:
        for e in d.get("episodes") or []:
            start = parse_hm(e.get("start"))
            if start is None or (turnaround <= start < dusk):
                continue                  # daytime: the sun muddies the ratio
            if (e.get("duration_s") or 0) < 600 or not e.get("wh") or not e.get("load_wh"):
                continue
            batt += e["wh"]
            load += e["load_wh"]
    if batt > 200 and load > 0:
        efficiency = min(1.0, max(0.6, load / batt))
        notes["efficiency"] = "%.0f Wh of load per %.0f Wh from the pack" % (load, batt)
    else:
        efficiency, notes["efficiency"] = float(fallback_efficiency), "fallback"

    # -- the hourly load: the median over the days, night hours filling gaps
    per_hour: list = [[] for _ in range(24)]
    for d in days:
        for h in d.get("hours") or []:
            if h.get("n") and h.get("load_w") is not None:
                per_hour[h["hour"]].append(float(h["load_w"]))
    hourly = [statistics.median(v) if v else None for v in per_hour]
    night = [w for h, w in enumerate(hourly)
             if w is not None and not (turnaround <= h * 60 < dusk)]
    fill = statistics.median(night) if night else float(default_load_w)
    missing = [h for h, w in enumerate(hourly) if w is None]
    hourly = [fill if w is None else w for w in hourly]
    if missing:
        notes["hourly_load"] = "%d hours filled with %.0f W" % (len(missing), fill)

    return Profile(int(turnaround), int(dusk), pack, efficiency, hourly,
                   days=len(days), notes=notes)


# --------------------------------------------------------------------------
# arithmetic on a profile
# --------------------------------------------------------------------------

def need_wh(profile: Profile, start: dt.datetime, end: dt.datetime) -> float:
    """Battery Wh the house is expected to draw between two moments.

    >>> prof = build_profile([_fake_day("2026-09-21")])
    >>> t0 = dt.datetime(2026, 9, 22, 23, 30)
    >>> round(need_wh(prof, t0, t0 + dt.timedelta(hours=1)))   # 125+150, /0.9
    306
    >>> need_wh(prof, t0, t0)
    0.0
    """
    if end <= start:
        return 0.0
    total = 0.0
    t = start
    while t < end:
        edge = (t.replace(minute=0, second=0, microsecond=0)
                + dt.timedelta(hours=1))
        stop = min(edge, end)
        total += profile.hourly_load_w[t.hour] * (stop - t).total_seconds() / 3600.0
        t = stop
    return total / profile.efficiency


def usable_wh(profile: Profile, soc, floor: float) -> float:
    """What the pack holds above the floor, in watt-hours.

    >>> usable_wh(build_profile([], fallback_pack_wh=2000), 80, 30)
    1000.0
    >>> usable_wh(build_profile([], fallback_pack_wh=2000), 20, 30)
    0.0
    """
    if soc is None:
        return 0.0
    return max(0.0, float(soc) - floor) / 100.0 * profile.pack_wh


def next_turnaround(profile: Profile, now: dt.datetime) -> dt.datetime:
    """The next moment the sun is expected to start lifting the pack.

    >>> prof = build_profile([], default_turnaround="07:00")
    >>> next_turnaround(prof, dt.datetime(2026, 9, 22, 23, 0)).isoformat()
    '2026-09-23T07:00:00'
    >>> next_turnaround(prof, dt.datetime(2026, 9, 22, 3, 0)).isoformat()
    '2026-09-22T07:00:00'
    """
    at = now.replace(hour=profile.turnaround_min // 60,
                     minute=profile.turnaround_min % 60, second=0, microsecond=0)
    if at <= now:
        at += dt.timedelta(days=1)
    return at


def in_window(profile: Profile, now: dt.datetime) -> bool:
    """Inside the sun's window: from the turnaround to dusk."""
    m = _minutes(now)
    return profile.turnaround_min <= m < profile.dusk_min


def night_of(profile: Profile, now: dt.datetime) -> dt.date:
    """Which evening the current night belongs to.

    >>> prof = build_profile([])
    >>> night_of(prof, dt.datetime(2026, 9, 22, 2, 0)).isoformat()
    '2026-09-21'
    >>> night_of(prof, dt.datetime(2026, 9, 22, 22, 0)).isoformat()
    '2026-09-22'
    """
    if _minutes(now) < profile.dusk_min:
        return now.date() - dt.timedelta(days=1)
    return now.date()


def release_time(profile: Profile, now: dt.datetime, soc, floor: float) -> dt.datetime | None:
    """When the pack can be released so it reaches the floor at the turnaround.

    The first moment from now at which the expected draw up to the
    turnaround fits in what is above the floor. `now` itself if it already
    fits; None when nothing is above the floor.

    >>> prof = build_profile([_fake_day("2026-09-21")])
    >>> now = dt.datetime(2026, 9, 22, 20, 0)
    >>> release_time(prof, now, 80, 30).strftime("%H:%M")     # 2400 Wh usable
    '22:40'
    >>> release_time(prof, now, 100, 30).strftime("%H:%M")    # 3360 Wh usable
    '21:00'
    >>> release_time(prof, now, 30, 30) is None
    True
    """
    usable = usable_wh(profile, soc, floor)
    if usable <= 0:
        return None
    target = next_turnaround(profile, now)
    t = now
    while t < target:
        if need_wh(profile, t, target) <= usable:
            return t
        t += dt.timedelta(minutes=STEP_MIN)
    return target


# --------------------------------------------------------------------------
# the governor -- the rule, with its two pieces of memory
# --------------------------------------------------------------------------

@dataclass
class Decision:
    want: int                          # SUB or SBU
    phase: str                         # floor | day | hold | night
    reason: str
    release_at: dt.datetime | None = None
    need_wh: float = 0.0
    usable_wh: float = 0.0
    floor_hold: bool = False
    in_window: bool = False
    turnaround_at: dt.datetime | None = None

    @property
    def name(self) -> str:
        return NAMES[self.want]


class Governor:
    """Turns time and SOC into SUB or SBU. Remembers two things: that the
    floor was hit (held until the sun lifts the pack past the resume
    level) and that tonight's reserve was released (held until the floor
    or the sun, so a wobble in the estimate cannot take it back).

    >>> prof = build_profile([_fake_day("2026-09-21")])
    >>> gov = Governor(floor=30, resume=35)
    >>> d = gov.decide(prof, dt.datetime(2026, 9, 22, 12, 0), 90)
    >>> d.name, d.phase
    ('SBU', 'day')
    >>> d = gov.decide(prof, dt.datetime(2026, 9, 22, 18, 0), 85)
    >>> d.name, d.phase, d.release_at.strftime("%H:%M")
    ('SUB', 'hold', '22:15')
    >>> d = gov.decide(prof, dt.datetime(2026, 9, 22, 23, 30), 85)
    >>> d.name, d.phase
    ('SBU', 'night')
    >>> gov.decide(prof, dt.datetime(2026, 9, 23, 1, 0), 60).phase    # latched
    'night'
    >>> d = gov.decide(prof, dt.datetime(2026, 9, 23, 6, 0), 30)
    >>> d.name, d.phase
    ('SUB', 'floor')
    >>> gov.decide(prof, dt.datetime(2026, 9, 23, 7, 30), 33).phase   # not yet
    'floor'
    >>> gov.decide(prof, dt.datetime(2026, 9, 23, 8, 0), 36).phase    # lifted
    'day'
    >>> gov.decide(prof, dt.datetime(2026, 9, 23, 17, 30), 100).phase   # a new night
    'hold'
    """

    def __init__(self, floor: float = 30, resume: float = 35):
        self.floor = float(floor)
        self.resume = max(float(resume), self.floor)
        self.floor_hold = False
        self.released_night: dt.date | None = None

    def decide(self, profile: Profile, now: dt.datetime, soc) -> Decision:
        window = in_window(profile, now)
        target = next_turnaround(profile, now)

        if soc is not None:
            if soc <= self.floor:
                self.floor_hold = True
            elif self.floor_hold and window and soc >= self.resume:
                self.floor_hold = False

        if self.floor_hold:
            return Decision(
                SUB, "floor",
                "pack at the floor (%.0f%%) - grid carries the house until the "
                "sun lifts it past %.0f%%" % (self.floor, self.resume),
                floor_hold=True, in_window=window, turnaround_at=target,
                usable_wh=usable_wh(profile, soc, self.floor))

        if window:
            return Decision(
                SBU, "day",
                "the sun's window (%s-%s) - solar and the pack carry the house"
                % (fmt_hm(profile.turnaround_min), fmt_hm(profile.dusk_min)),
                in_window=True, turnaround_at=target)

        night = night_of(profile, now)
        usable = usable_wh(profile, soc, self.floor)
        need = need_wh(profile, now, target)
        if self.released_night == night or (soc is not None and need <= usable):
            self.released_night = night
            return Decision(
                SBU, "night",
                "reserve released - the pack carries the house to the sun (~%s)"
                % fmt_hm(profile.turnaround_min),
                release_at=now, need_wh=need, usable_wh=usable, turnaround_at=target)

        release = release_time(profile, now, soc, self.floor)
        if release is None or release >= target:
            why = ("holding the reserve for an outage - too little above the "
                   "floor to release tonight")
            release = None
        else:
            why = ("holding the reserve for an outage - release to the pack ~%s"
                   % release.strftime("%H:%M"))
        return Decision(SUB, "hold", why, release_at=release, need_wh=need,
                        usable_wh=usable, turnaround_at=target)


def headline(payload: dict | None) -> str | None:
    """One short line for the tray menu and the watch screen, from the
    payload the collector publishes in state.json.

    >>> headline({"enabled": False, "want": "SUB", "phase": "hold", "release_at": "01:10"})
    'Would set SUB: reserve held, release ~01:10'
    >>> headline({"enabled": True, "want": "SBU", "phase": "night"})
    'Auto SBU: reserve released, pack to the sun'
    >>> headline(None) is None
    True
    """
    if not payload or not payload.get("want"):
        return None
    phase = payload.get("phase")
    if phase == "floor":
        what = "at the floor, grid until the sun"
    elif phase == "day":
        what = "the sun's window"
    elif phase == "night":
        what = "reserve released, pack to the sun"
    elif payload.get("release_at"):
        what = "reserve held, release ~%s" % payload["release_at"]
    else:
        what = "reserve held all night"
    lead = "Auto" if payload.get("enabled") else "Would set"
    return "%s %s: %s" % (lead, payload["want"], what)


# --------------------------------------------------------------------------
# doctest fixture
# --------------------------------------------------------------------------

def _fake_day(date: str, soc_min_at: str = "07:14", pv_last: str = "17:26") -> dict:
    """A day analysis shaped like insights.analyse_day's, for the doctests:
    300 W before dawn dropping to 100 W in the small hours, a kilowatt by
    day, half a kilowatt in the evening; sun 07-17 peaking at noon; one
    night episode implying a 4.8 kWh pack at 90 % efficiency."""
    hours = []
    for h in range(24):
        if 3 <= h < 6:
            load = 100
        elif 7 <= h < 17:
            load = 1000
        elif 17 <= h < 23:
            load = 500
        elif h == 23:
            load = 250
        else:
            load = 300
        pv = 0 if not (7 <= h < 17) else int(2400 * (1 - abs(12.5 - h) / 6))
        hours.append({"hour": h, "n": 100, "load_w": load, "pv_w": pv})
    return {"date": date, "samples": 2400, "soc_min_at": soc_min_at,
            "pv_first": "07:00", "pv_last": pv_last, "hours": hours,
            "episodes": [{"start": "02:00", "end": "07:00", "duration_s": 18000,
                          "wh": 1000, "load_wh": 900, "soc_drop": 21,
                          "implied_pack_wh": 4800}]}


if __name__ == "__main__":
    import doctest
    print(doctest.testmod())
