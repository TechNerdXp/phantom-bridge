"""Output-priority policy: SBU by day, SUB from dusk, SBU again once the
pack can carry the rest of the night, and SUB at the floor.

The owner's rule (2026-09-22), which this replaces the inverter's own timer
(menu 99) with:

  * When the sun goes down -- the data says when, not the clock -- switch
    to SUB. The house runs on the grid and the pack is kept as the reserve
    an outage will need. If the grid drops, the inverter uses the pack
    regardless; the setting only says who carries the house while the
    grid is there.
  * The evening belongs to the reserve. For AUTO_EVENING_RESERVE_H after
    dusk the pack is not spent on the house however full it is: the
    outages cluster in those hours and the heavy motors run then.
  * After that, spending the pack beats letting it sit -- the number
    slides either way, so it may as well slide carrying the house. Release
    (SBU) at the moment that LANDS the pack on the floor exactly at the
    turnaround: nothing left over, nothing short (owner, 2026-09-23).
  * And if the load would eat into the morning, stop: hand the house back
    to the grid with the night's planned spend done. That is the floor
    rule in-night, and the stand-down when a trim says to spend less.

    The night is settled in SOC POINTS, not watt-hours. Points because the
    pack's Wh scale is the one figure nobody has (Oracle #29): pack_wh sits
    pinned at the 2000 Wh cap, 20 Wh a point, while the logs measure 48-55
    Wh a point out, so the watt-hour chain thought the pack 2.5x emptier
    than it is and held the release hours too late (2026-09-23: it wanted
    06:10 where the points say 04:10). Points per kWh of house load is
    measured straight off LAST NIGHT's episodes -- "last night is our
    light", the same reading the turnaround and the dusk get -- and folds
    the inverter's own losses in with it.

    Every night's landing then patches the next: the miss against the floor
    is carried as a trim, in points, half of it per night and clamped.
    Points left unspent widen tomorrow's window at the FRONT (release
    earlier); a landing under the floor shortens it at the BACK (stand down
    before dawn), so the correction never eats the evening reserve. The
    patch is sized by the load at the hours it moves, never by the clock:
    an hour at 600 W costs three times an hour at 200 W.

    AUTO_RELEASE_LATEST is a BACKSTOP, not a ceiling: with an SOC to read
    the landing rule decides, and the clock only applies when there is no
    SOC at all. It was the rule while the night was settled in watt-hours
    and the arithmetic could not be trusted to let go.
  * At the floor, at any hour, SUB: the grid carries the house and the
    pack sits until the turnaround, and then until the sun has lifted it
    back over the resume level. Then SBU, the day's setting.

  * A request from outside (Switch-X writes logs/request.json: want,
    until, why) is its own phase, "requested": ahead of the plan, never
    past the floor rule, capped in length so a stuck file cannot keep the
    pack off all night, ignored if the grid was absent when it arrived.
  * The release keeps a margin: the plan lets go only when what is above
    the floor covers the expected draw AND the margin, so the bursts
    Switch-X grants after the release (the motor, the cooker) do not push
    the pack to the floor before the turnaround. What is left over is
    published as headroom.

Pure: no link, no files, no clock of its own. build_profile() turns day
analyses (insights.analyse_day, via days.DayStore) into a Profile; a
Governor turns a Profile, the time, the SOC and any Request into a
Decision. Doctested. Time-of-day values are minutes after midnight, site
time.
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

# What counts as an episode worth reading the drain rate from: long enough
# to average out, deep enough that the SOC's whole-point steps are not the
# signal, and not starting at the top of the scale, where the number falls
# far faster than the pack does (97 -> 84 in 34 minutes, 2026-09-20) because
# it is clamped at 100 and has nowhere to sit.
MIN_EPISODE_S = 20 * 60
MIN_DROP_POINTS = 3
TOP_OF_SCALE = 90

# An outage this long spent the pack for reasons of its own, so that night's
# landing says nothing about the plan and is not carried into the trim.
OUTAGE_SPOILS_S = 30 * 60

# How far yesterday's turnaround may sit from the recent median before it is
# distrusted and the median used instead. The second of the two guards on
# it; the first is the sun itself (_dawn_low).
TURNAROUND_DRIFT_MIN = 90

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
    points_per_kwh: float = 0.0      # SOC points spent per kWh the house draws
    days: int = 0                    # finished days it was read from
    notes: dict = field(default_factory=dict)   # where each figure came from
    built_for: dt.date | None = None            # the day it was built on

    def as_dict(self) -> dict:
        return {"turnaround": fmt_hm(self.turnaround_min),
                "dusk": fmt_hm(self.dusk_min),
                "pack_wh": round(self.pack_wh),
                "points_per_kwh": round(self.points_per_kwh, 2),
                "efficiency": round(self.efficiency, 3),
                "hourly_load_w": [round(w) for w in self.hourly_load_w],
                "days": self.days, "notes": dict(self.notes),
                "built_for": self.built_for.isoformat() if self.built_for else None}


def _in_window(minute, turnaround: int, dusk: int) -> bool:
    """Is that time of day inside the sun's window?"""
    return minute is not None and turnaround <= minute < dusk


def _dawn_low(day: dict):
    """A day's turnaround candidate: the minute its SOC bottomed out, but
    only when the SUN is what lifted it again.

    The pack rises for other reasons -- a utility charge, rare on this site
    but not impossible -- and that rise is not a sunrise. A low before the
    day's first PV therefore says nothing about the sun and is thrown out,
    and so is one outside the dawn window entirely.

    >>> _dawn_low({"soc_min_at": "07:14", "pv_first": "06:52"})
    434
    >>> _dawn_low({"soc_min_at": "04:10", "pv_first": "06:52"}) is None  # charged
    True
    >>> _dawn_low({"soc_min_at": "21:30", "pv_first": "06:52"}) is None  # not dawn
    True
    >>> _dawn_low({"soc_min_at": "07:14"})                    # no PV record
    434
    >>> _dawn_low({}) is None
    True
    """
    m = parse_hm((day or {}).get("soc_min_at"))
    if m is None or not (MORNING[0] <= m < MORNING[1]):
        return None
    first_sun = parse_hm(day.get("pv_first"))
    if first_sun is not None and m < first_sun:
        return None
    return m


def build_profile(days: list, *, default_turnaround: str = "07:00",
                  default_dusk: str = "17:00", default_load_w: float = 400.0,
                  dusk_fraction: float = 0.25, pack_wh: float | None = None,
                  capacity_ah: float | None = None,
                  fallback_pack_wh: float = 2000.0,
                  pack_cap_wh: float | None = None,
                  fallback_efficiency: float = 0.88,
                  fallback_points_per_kwh: float = 19.0) -> Profile:
    """Read the figures out of day analyses, most recent first.

    Each entry is what insights.analyse_day returns for one finished day.
    Yesterday alone gives the turnaround and the dusk ("just like
    yesterday"); the whole list gives the hourly load, the pack size the
    episodes imply and the battery-to-load efficiency. Anything missing
    falls back to the defaults, and `notes` says which.

    `pack_cap_wh` is the practical ceiling on a derived pack figure: the
    episodes read the inverter's voltage-derived SOC, which on this site
    implies the sold 5 kWh while the pack delivers about 2 kWh in practice
    (Oracle #29). The data may say less than the cap, never more. An
    explicit `pack_wh` pins the figure and is not capped.

    >>> prof = build_profile([])
    >>> prof.as_dict()["turnaround"], prof.as_dict()["dusk"], prof.pack_wh
    ('07:00', '17:00', 2000.0)
    >>> day = _fake_day("2026-09-21", soc_min_at="07:14", pv_last="17:26")
    >>> prof = build_profile([day])
    >>> fmt_hm(prof.turnaround_min), fmt_hm(prof.dusk_min), prof.days
    ('07:14', '17:00', 1)
    >>> prof.pack_wh, round(prof.efficiency, 2)
    (4800.0, 0.9)
    >>> prof = build_profile([day], pack_cap_wh=2000)
    >>> prof.pack_wh, prof.notes["pack_wh"]
    (2000.0, 'median of 1 episodes implies 4800 Wh, capped at the practical 2000 Wh')
    >>> [prof.hourly_load_w[h] for h in (0, 3, 12, 20)]
    [300.0, 100.0, 1000.0, 500.0]

    The turnaround has two guards. A pack lifted before the sun was lifted
    by something else -- a utility charge -- so that low is not a sunrise
    and the day falls through to its first PV instead::

        >>> prof = build_profile([_fake_day("2026-09-22", soc_min_at="04:30")])
        >>> fmt_hm(prof.turnaround_min), prof.notes["turnaround"]
        ('07:00', 'first sun 2026-09-22 at 07:00')
        >>> prof.notes["turnaround_dropped"]
        ['2026-09-22: low at 04:30, first sun 07:00']

    And a candidate far from the last few days is distrusted in favour of
    their median, so one odd morning cannot drag the day's frame::

        >>> prof = build_profile([_fake_day("2026-09-22", soc_min_at="10:30"),
        ...                       _fake_day("2026-09-21", soc_min_at="07:14"),
        ...                       _fake_day("2026-09-20", soc_min_at="07:10"),
        ...                       _fake_day("2026-09-19", soc_min_at="07:20")])
        >>> fmt_hm(prof.turnaround_min)
        '07:17'
        >>> prof.notes["turnaround"]
        'lowest SOC 2026-09-22 at 10:30 is 193 min off the 4-day median - using the median 07:17'
    """
    notes: dict = {}
    days = [d for d in (days or []) if d and d.get("samples")]

    # -- the turnaround: yesterday's lowest SOC, if the SUN is what lifted
    # the pack. A utility charge does it too -- rare here, but it happens --
    # and would otherwise be read as a sunrise and drag the whole day's
    # frame hours early. Two guards (owner, 2026-09-23): the sun itself,
    # so a low before that day's first PV is thrown out, and the last few
    # days, so a candidate far from their median is distrusted in favour
    # of the median.
    turnaround = None
    seen = [(d, _dawn_low(d)) for d in days]
    for d, m in seen:
        if m is None and parse_hm(d.get("soc_min_at")) is not None:
            notes.setdefault("turnaround_dropped", []).append(
                "%s: low at %s, first sun %s" % (d.get("date"), d.get("soc_min_at"),
                                                 d.get("pv_first") or "none"))
    valid = [(d, m) for d, m in seen if m is not None]
    if valid:
        d, m = valid[0]
        mid = statistics.median([x for _, x in valid])
        if len(valid) >= 3 and abs(m - mid) > TURNAROUND_DRIFT_MIN:
            turnaround = int(mid)
            notes["turnaround"] = (
                "lowest SOC %s at %s is %d min off the %d-day median - "
                "using the median %s" % (d.get("date"), d.get("soc_min_at"),
                                         abs(m - mid), len(valid), fmt_hm(mid)))
        else:
            turnaround = m
            notes["turnaround"] = "lowest SOC %s at %s" % (d.get("date"), d.get("soc_min_at"))
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
        if pack_cap_wh and pack > float(pack_cap_wh):
            notes["pack_wh"] = "%s implies %.0f Wh, capped at the practical %.0f Wh" % (
                notes["pack_wh"], pack, float(pack_cap_wh))
            notes["pack_wh_uncapped"] = round(pack)
            pack = float(pack_cap_wh)

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

    # -- the rate: SOC points the pack gives up per kWh the house draws.
    # This is the currency the release is settled in, and the reason it is
    # not watt-hours: the pack's Wh scale is the one figure nobody has
    # (Oracle #29), while points per kWh is measured directly and folds the
    # inverter's own losses in with it. LAST NIGHT is the light -- the same
    # "just like yesterday" the turnaround and the dusk are read with -- so
    # the most recent night with usable episodes wins outright, not a
    # blend across the week.
    points = None
    for d in days:
        seen = [e["soc_drop"] / (e["load_wh"] / 1000.0)
                for e in (d.get("episodes") or [])
                if (e.get("soc_drop") or 0) >= MIN_DROP_POINTS
                and (e.get("load_wh") or 0) > 0
                and (e.get("duration_s") or 0) >= MIN_EPISODE_S
                and (e.get("soc_start") or 0) <= TOP_OF_SCALE
                and not _in_window(parse_hm(e.get("start")), turnaround, dusk)]
        if seen:
            points = statistics.median(seen)
            notes["points_per_kwh"] = "%d night episode(s) on %s" % (len(seen), d.get("date"))
            break
    if points is None:
        points = float(fallback_points_per_kwh)
        notes["points_per_kwh"] = "fallback"

    return Profile(int(turnaround), int(dusk), pack, efficiency, hourly,
                   points_per_kwh=float(points), days=len(days), notes=notes)


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


# --------------------------------------------------------------------------
# the same arithmetic in SOC points -- the currency the plan is settled in
# --------------------------------------------------------------------------
#
# The owner's rule, 2026-09-23: aim to LAND on the floor exactly at the
# turnaround, nothing left over and nothing short. Points, not watt-hours,
# because the pack's Wh scale is the one number nobody has (Oracle #29) and
# the watt-hour chain gets it wrong in both directions at once -- pack_wh is
# pinned at the 2000 Wh cap (20 Wh a point) while the logs measure 48-55 Wh
# a point out, so it thinks the pack is 2.5x emptier than it is and holds
# the release hours too late. Points per kWh is measured straight off last
# night and folds the inverter's losses in with it.
#
#   cost    the points the house will spend over a span
#   budget  the points there are to spend: soc - floor, plus any trim
#   window  release -> stand-down; normally stand-down IS the turnaround
#   trim    yesterday's landing miss, carried (see trim_step)

def cost_points(profile: Profile, start: dt.datetime, end: dt.datetime) -> float:
    """SOC points the house is expected to spend between two moments.

    >>> prof = build_profile([_fake_day("2026-09-21")])
    >>> round(prof.points_per_kwh, 1)                       # 21 points / 0.9 kWh
    23.3
    >>> t0 = dt.datetime(2026, 9, 22, 23, 30)
    >>> round(cost_points(prof, t0, t0 + dt.timedelta(hours=1)), 1)   # 125+150 Wh
    6.4
    >>> cost_points(prof, t0, t0)
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
    return total / 1000.0 * profile.points_per_kwh


def budget_points(soc, floor: float, trim: float = 0.0) -> float:
    """The points there are to spend. A positive trim widens the window at
    the front -- yesterday landed above the floor, so start earlier; a
    negative one is spent at the back instead, by stand_down_at.

    >>> budget_points(64, 30), budget_points(64, 30, trim=6)
    (34.0, 40.0)
    >>> budget_points(20, 30), budget_points(None, 30)
    (0.0, 0.0)
    """
    if soc is None:
        return 0.0
    return max(0.0, float(soc) - floor) + max(0.0, trim)


def stand_down_at(profile: Profile, turnaround: dt.datetime,
                  trim: float = 0.0) -> dt.datetime:
    """When the pack hands the house back, which is the turnaround unless a
    negative trim shortens the window at the back -- yesterday landed under
    the floor, so stop short of dawn by that many points. Walked back
    through the load, not the clock: an hour at 600 W costs three times an
    hour at 200 W, so the patch is sized by what those hours actually draw.

    >>> prof = build_profile([_fake_day("2026-09-21")])
    >>> t = dt.datetime(2026, 9, 23, 7, 14)
    >>> stand_down_at(prof, t).strftime("%H:%M")            # no trim, no change
    '07:14'
    >>> stand_down_at(prof, t, trim=-6).strftime("%H:%M")   # 6 points off the end
    '06:54'
    >>> stand_down_at(prof, t, trim=+6).strftime("%H:%M")   # a widening goes up front
    '07:14'

    The same six points, sized by the load at the hours being given back:
    twenty minutes of a 1 kW dawn, two and a half hours of a 100 W night::

        >>> stand_down_at(prof, dt.datetime(2026, 9, 23, 6, 0),
        ...               trim=-6).strftime("%H:%M")
        '03:25'
    """
    owed = -min(0.0, trim)
    if owed <= 0:
        return turnaround
    t = turnaround
    while owed > 0 and turnaround - t < dt.timedelta(hours=12):
        step = t - dt.timedelta(minutes=STEP_MIN)
        owed -= cost_points(profile, step, t)
        t = step
    return t


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


def latest_release_at(profile: Profile, now: dt.datetime,
                      latest_min: int | None) -> dt.datetime | None:
    """The moment tonight's reserve is released regardless: the latest
    clock on the current night. A clock before dusk (02:00) is the small
    hours of the next morning; one after dusk (23:00) is the same evening.

    >>> prof = build_profile([], default_dusk="17:00")
    >>> latest_release_at(prof, dt.datetime(2026, 9, 22, 22, 0), 2 * 60).isoformat()
    '2026-09-23T02:00:00'
    >>> latest_release_at(prof, dt.datetime(2026, 9, 23, 6, 0), 2 * 60).isoformat()
    '2026-09-23T02:00:00'
    >>> latest_release_at(prof, dt.datetime(2026, 9, 22, 22, 0), 23 * 60).isoformat()
    '2026-09-22T23:00:00'
    >>> latest_release_at(prof, dt.datetime(2026, 9, 22, 22, 0), None) is None
    True
    """
    if latest_min is None:
        return None
    day = night_of(profile, now)
    if latest_min < profile.dusk_min:
        day += dt.timedelta(days=1)
    return dt.datetime(day.year, day.month, day.day, latest_min // 60, latest_min % 60)


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


def release_time(profile: Profile, now: dt.datetime, soc, floor: float,
                 margin_wh: float = 0.0) -> dt.datetime | None:
    """When the pack can be released so it reaches the floor at the turnaround.

    The first moment from now at which the expected draw up to the
    turnaround, plus the margin, fits in what is above the floor. `now`
    itself if it already fits; None when nothing is above the floor.

    >>> prof = build_profile([_fake_day("2026-09-21")])
    >>> now = dt.datetime(2026, 9, 22, 20, 0)
    >>> release_time(prof, now, 80, 30).strftime("%H:%M")     # 2400 Wh usable
    '22:40'
    >>> release_time(prof, now, 100, 30).strftime("%H:%M")    # 3360 Wh usable
    '21:00'
    >>> release_time(prof, now, 30, 30) is None
    True
    >>> release_time(prof, now, 80, 30, margin_wh=300).strftime("%H:%M")   # later
    '23:25'
    """
    usable = usable_wh(profile, soc, floor)
    if usable <= 0:
        return None
    target = next_turnaround(profile, now)
    t = now
    while t < target:
        if need_wh(profile, t, target) + margin_wh <= usable:
            return t
        t += dt.timedelta(minutes=STEP_MIN)
    return target


def release_at_points(profile: Profile, now: dt.datetime, soc, floor: float,
                      trim: float = 0.0, margin_points: float = 0.0,
                      end: dt.datetime | None = None) -> dt.datetime | None:
    """When to hand the house to the pack so it LANDS on the floor exactly
    at the end of the window -- the owner's rule of 2026-09-23, settled in
    SOC points.

    The first moment from now whose cost to the end of the window fits the
    budget. `now` itself when it already fits, meaning the pack cannot be
    spent in time and should start at once; None when there is nothing
    above the floor to spend.

    >>> prof = build_profile([_fake_day("2026-09-21")])
    >>> now = dt.datetime(2026, 9, 22, 20, 0)
    >>> release_at_points(prof, now, 80, 30).strftime("%H:%M")
    '22:45'
    >>> release_at_points(prof, now, 100, 30).strftime("%H:%M")   # more to spend
    '21:00'
    >>> release_at_points(prof, now, 30, 30) is None              # nothing above it
    True

    A trim from yesterday's landing widens the window at the front::

        >>> release_at_points(prof, now, 80, 30, trim=+8).strftime("%H:%M")
        '22:00'

    A negative one is not spent here -- it comes off the back, in
    stand_down_at, so the release itself does not move::

        >>> release_at_points(prof, now, 80, 30, trim=-8).strftime("%H:%M")
        '22:45'
    """
    budget = budget_points(soc, floor, trim) - max(0.0, margin_points)
    if budget <= 0:
        return None
    target = end or next_turnaround(profile, now)
    t = now
    while t < target:
        if cost_points(profile, t, target) <= budget:
            return t
        t += dt.timedelta(minutes=STEP_MIN)
    return target


def trim_step(day: dict, floor: float, gain: float = 0.5) -> float | None:
    """What a finished night's landing says to carry into the next one.

    The miss is the SOC at the turnaround against the floor we aimed at:
    landed above it and there were points left unspent, so widen the window
    (positive); landed under it and it was spent too fast, so shorten it
    (negative). Half the miss by default, so one odd night cannot swing the
    plan. None when the day cannot say -- no dawn low, or an outage spent
    the pack for reasons of its own.

    >>> trim_step({"soc_min": 36, "soc_min_at": "07:14"}, 30)
    3.0
    >>> trim_step({"soc_min": 26, "soc_min_at": "07:14"}, 30)
    -2.0
    >>> trim_step({"soc_min": 30, "soc_min_at": "07:14"}, 30)
    0.0
    >>> trim_step({"soc_min": 36, "soc_min_at": "21:00"}, 30) is None     # not a dawn low
    True
    >>> trim_step({"soc_min": 12, "soc_min_at": "07:14", "outage_s": 9000}, 30) is None
    True
    >>> trim_step({}, 30) is None
    True
    """
    if not day or day.get("soc_min") is None:
        return None
    at = parse_hm(day.get("soc_min_at"))
    if at is None or not (MORNING[0] <= at < MORNING[1]):
        return None
    if (day.get("outage_s") or 0) >= OUTAGE_SPOILS_S:
        return None
    return round((float(day["soc_min"]) - float(floor)) * gain, 2)


# --------------------------------------------------------------------------
# the governor -- the rule, with its two pieces of memory
# --------------------------------------------------------------------------

@dataclass
class Request:
    """An outside ask for a setting until a time of day, from
    logs/request.json. `parse_request` builds one."""
    want: int                          # SUB or SBU
    until: dt.datetime                 # site time, naive
    why: str = ""

    @property
    def key(self) -> tuple:
        return (self.want, self.until.isoformat(timespec="minutes"), self.why)

    @property
    def name(self) -> str:
        return NAMES[self.want]


def parse_request(payload, now: dt.datetime) -> Request | None:
    """{"want": "SUB", "until": "05:30", "why": "geyser for Fajr"} -> Request.

    `until` is a time of day; it is taken as today unless that is more than
    twelve hours in the past, when it means tomorrow (a request written at
    23:50 until 00:20 crosses midnight). Junk, an unknown setting or an
    `until` already passed give None.

    >>> now = dt.datetime(2026, 9, 22, 4, 0)
    >>> r = parse_request({"want": "SUB", "until": "05:30", "why": "geyser"}, now)
    >>> r.name, r.until.isoformat(timespec="minutes"), r.why
    ('SUB', '2026-09-22T05:30', 'geyser')
    >>> parse_request({"want": "sbu", "until": "00:20"}, dt.datetime(2026, 9, 22, 23, 50)).until.day
    23
    >>> parse_request({"want": "SUB", "until": "03:00"}, now) is None      # passed
    True
    >>> parse_request({"want": "UTI", "until": "05:30"}, now) is None      # not ours
    True
    >>> parse_request("junk", now) is None
    True
    """
    if not isinstance(payload, dict):
        return None
    want = payload.get("want")
    if isinstance(want, str):
        want = {"SUB": SUB, "SBU": SBU}.get(want.strip().upper())
    if want not in (SUB, SBU):
        return None
    minutes = parse_hm(payload.get("until"))
    if minutes is None:
        return None
    until = now.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)
    if until < now - dt.timedelta(hours=12):
        until += dt.timedelta(days=1)
    if until <= now:
        return None
    why = payload.get("why")
    return Request(want, until, str(why) if why else "")


@dataclass
class Decision:
    want: int                          # SUB or SBU
    phase: str                         # floor | requested | day | hold | night
    reason: str
    release_at: dt.datetime | None = None
    need_wh: float = 0.0
    usable_wh: float = 0.0
    trim: float = 0.0                  # points carried from last night's landing
    budget: float = 0.0                # points there are to spend
    cost: float = 0.0                  # points the rest of the window will take
    stand_down: dt.datetime | None = None   # the window's back edge, if pulled in
    rate: float = 0.0                  # the profile's points per kWh, for the Wh views
    floor_hold: bool = False
    in_window: bool = False
    turnaround_at: dt.datetime | None = None
    until: dt.datetime | None = None   # requested: when the hold ends
    request: dict | None = None        # what became of the request, if any

    @property
    def name(self) -> str:
        return NAMES[self.want]

    @property
    def headroom_wh(self) -> float:
        """What is above the floor beyond the expected draw to the
        turnaround: the watt-hours an outside caller may spend on the pack
        without pushing the plan to the floor early."""
        if self.rate > 0:
            return max(0.0, self.budget - self.cost) / self.rate * 1000.0
        return max(0.0, self.usable_wh - self.need_wh)


class Governor:
    """Turns time and SOC into SUB or SBU. Remembers two things: that the
    floor was hit (held until the sun lifts the pack past the resume
    level) and that tonight's reserve was released (held until the floor
    or the sun, so a wobble in the estimate cannot take it back). And a
    third, when there is one: the request it is honouring, with when it
    first saw it, so the cap on a hold counts from then.

    >>> prof = build_profile([_fake_day("2026-09-21")])
    >>> gov = Governor(floor=30, resume=35)
    >>> d = gov.decide(prof, dt.datetime(2026, 9, 22, 12, 0), 90)
    >>> d.name, d.phase
    ('SBU', 'day')
    >>> d = gov.decide(prof, dt.datetime(2026, 9, 22, 18, 0), 85)
    >>> d.name, d.phase, d.release_at.strftime("%H:%M")
    ('SUB', 'hold', '22:20')
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

    A request rides over the plan, not over the floor, and is capped:

    >>> gov = Governor(floor=30, resume=35, request_max_min=60)
    >>> ask = parse_request({"want": "SUB", "until": "05:30", "why": "geyser"},
    ...                     dt.datetime(2026, 9, 23, 1, 0))
    >>> d = gov.decide(prof, dt.datetime(2026, 9, 23, 1, 0), 85, request=ask)
    >>> d.name, d.phase, d.until.strftime("%H:%M"), d.request["state"]
    ('SUB', 'requested', '02:00', 'honoured')
    >>> d = gov.decide(prof, dt.datetime(2026, 9, 23, 2, 30), 80, request=ask)
    >>> d.phase, d.request["state"]                                    # cap passed
    ('night', 'expired')
    >>> d = gov.decide(prof, dt.datetime(2026, 9, 23, 3, 0), 25, request=ask)
    >>> d.phase                                                        # floor wins
    'floor'
    >>> gov2 = Governor(floor=30, resume=35)
    >>> d = gov2.decide(prof, dt.datetime(2026, 9, 23, 1, 0), 85, request=ask,
    ...                 grid_present=False)
    >>> d.phase, d.request["state"]
    ('night', 'ignored')
    >>> gov2.decide(prof, dt.datetime(2026, 9, 23, 1, 5), 85, request=ask,
    ...             grid_present=True).request["state"]                # still
    'ignored'

    The latest-release clock overrides the arithmetic: a small pack that
    could never satisfy the sum is still released at that hour and runs
    to the floor, exactly as the panel's timer did:

    >>> gov4 = Governor(floor=30, resume=35, margin_wh=300, latest_release="02:00")
    >>> gov4.decide(prof, dt.datetime(2026, 9, 22, 23, 0), 40).phase
    'hold'
    >>> gov4.decide(prof, dt.datetime(2026, 9, 23, 2, 0), 40).phase     # still holding
    'hold'
    >>> gov4.decide(prof, dt.datetime(2026, 9, 23, 2, 0), None).phase   # blind: the clock
    'night'
    >>> gov4.decide(prof, dt.datetime(2026, 9, 23, 4, 0), 30).phase      # the floor
    'floor'
    >>> gov5 = Governor(floor=30, resume=35, latest_release="02:00")
    >>> gov5.decide(prof, dt.datetime(2026, 9, 22, 23, 30), 85).phase   # earlier still
    'night'

    Last night's landing is carried as a trim, in SOC points. Left points
    unspent and the window widens at the front, so the release walks
    earlier; went under the floor and it shortens at the back instead, and
    the pack stands down before the sun:

    >>> gov6 = Governor(floor=30, resume=35, trim=+8)
    >>> gov6.decide(prof, dt.datetime(2026, 9, 22, 20, 0), 80).release_at.strftime("%H:%M")
    '22:00'
    >>> gov7 = Governor(floor=30, resume=35, trim=-8)
    >>> d = gov7.decide(prof, dt.datetime(2026, 9, 22, 20, 0), 80)
    >>> d.release_at.strftime("%H:%M"), d.stand_down.strftime("%H:%M")
    ('22:45', '06:34')
    >>> gov7.decide(prof, dt.datetime(2026, 9, 22, 23, 0), 78).phase
    'night'
    >>> gov7.decide(prof, dt.datetime(2026, 9, 23, 6, 45), 40).phase
    'stand-down'

    And the evening belongs to the reserve whatever the pack holds -- a
    light winter night would otherwise release at dusk, into the hours the
    outages and the motors are in:

    >>> gov8 = Governor(floor=30, resume=35, evening_reserve_h=4)
    >>> gov8.decide(prof, dt.datetime(2026, 9, 22, 18, 0), 100).phase
    'hold'
    >>> gov8.decide(prof, dt.datetime(2026, 9, 22, 18, 0), 100).release_at.strftime("%H:%M")
    '21:00'

    The margin holds the release back until the bursts fit too:

    >>> gov3 = Governor(floor=30, resume=35, margin_wh=300)
    >>> gov3.decide(prof, dt.datetime(2026, 9, 22, 22, 45), 80).phase
    'hold'
    >>> d = gov3.decide(prof, dt.datetime(2026, 9, 22, 23, 40), 80)
    >>> d.phase, round(d.headroom_wh) >= 300
    ('night', True)
    """

    def __init__(self, floor: float = 30, resume: float = 35,
                 margin_wh: float = 0.0, request_max_min: float = 60,
                 latest_release: str | None = None, trim: float = 0.0,
                 evening_reserve_h: float = 0.0):
        self.floor = float(floor)
        self.resume = max(float(resume), self.floor)
        self.margin_wh = max(0.0, float(margin_wh))
        self.trim = float(trim)          # set daily from the last landing
        # The evening belongs to the reserve: outages cluster after dusk and
        # the motors run then, so the pack is not spent on the house no
        # matter how full it is (owner, 2026-09-23).
        self.evening_reserve = dt.timedelta(hours=max(0.0, float(evening_reserve_h)))
        self.latest_min = parse_hm(latest_release)
        self.request_max = dt.timedelta(minutes=max(1.0, float(request_max_min)))
        self.floor_hold = False
        self.released_night: dt.date | None = None
        self._request_key: tuple | None = None
        self._request_seen: dt.datetime | None = None
        self._request_ignored = False

    def _weigh_request(self, request: Request | None, now: dt.datetime,
                       grid_present: bool) -> tuple[dt.datetime | None, dict | None]:
        """The effective end of the hold (None if there is nothing to
        honour) and the `request` payload for the record."""
        if request is None:
            self._request_key = self._request_seen = None
            self._request_ignored = False
            return None, None
        if request.key != self._request_key:
            self._request_key = request.key
            self._request_seen = now
            self._request_ignored = not grid_present
        record = {"want": request.name, "why": request.why,
                  "until": request.until.strftime("%H:%M")}
        if self._request_ignored:
            record["state"] = "ignored"
            record["note"] = "the grid was absent when it arrived"
            return None, record
        until = min(request.until, self._request_seen + self.request_max)
        record["until"] = until.strftime("%H:%M")
        if until < request.until:
            record["note"] = "capped at %d minutes" % (self.request_max.total_seconds() // 60)
        if now >= until:
            record["state"] = "expired"
            return None, record
        record["state"] = "honoured"
        return until, record

    def decide(self, profile: Profile, now: dt.datetime, soc,
               request: Request | None = None, grid_present: bool = True) -> Decision:
        window = in_window(profile, now)
        target = next_turnaround(profile, now)
        until, asked = self._weigh_request(request, now, grid_present)

        if soc is not None:
            if soc <= self.floor:
                self.floor_hold = True
            elif self.floor_hold and window and soc >= self.resume:
                self.floor_hold = False

        usable = usable_wh(profile, soc, self.floor)
        need = 0.0 if window else need_wh(profile, now, target)

        if self.floor_hold:
            return Decision(
                SUB, "floor",
                "pack at the floor (%.0f%%) - grid carries the house until the "
                "sun lifts it past %.0f%%" % (self.floor, self.resume),
                floor_hold=True, in_window=window, turnaround_at=target,
                usable_wh=usable, need_wh=need, request=asked)

        if until is not None:
            return Decision(
                request.want, "requested",
                "requested: %s until %s%s" % (
                    request.name, until.strftime("%H:%M"),
                    " (%s)" % request.why if request.why else ""),
                in_window=window, turnaround_at=target, until=until,
                usable_wh=usable, need_wh=need, request=asked)

        note = ""
        if asked and asked["state"] == "ignored":
            note = " - request for %s ignored, %s" % (asked["want"], asked["note"])
        elif asked and asked["state"] == "expired":
            note = " - request for %s expired at %s" % (asked["want"], asked["until"])

        if window:
            return Decision(
                SBU, "day",
                "the sun's window (%s-%s) - solar and the pack carry the house%s"
                % (fmt_hm(profile.turnaround_min), fmt_hm(profile.dusk_min), note),
                in_window=True, turnaround_at=target, usable_wh=usable, request=asked)

        # -- the night, settled in SOC points: land on the floor at the end
        # of the window, nothing left over and nothing short.
        night = night_of(profile, now)
        latest = latest_release_at(profile, now, self.latest_min)
        end = stand_down_at(profile, target, self.trim)
        # the evening reserve: the earliest the pack may be spent tonight
        guard = dt.datetime.combine(night, dt.time()) + dt.timedelta(
            minutes=profile.dusk_min) + self.evening_reserve
        # one knob, in watt-hours, because that is what Switch-X spends;
        # priced into points at the rate last night measured
        margin_points = self.margin_wh / 1000.0 * profile.points_per_kwh
        # the whole budget is what the pack holds above the floor; the
        # release may only commit what is left after the margin, and the
        # difference is what gets published as headroom
        whole = budget_points(soc, self.floor, self.trim)
        budget = whole - margin_points
        # The release is always worked out to the turnaround: a negative
        # trim must make the night spend LESS, not spend the same over a
        # window shifted earlier, so it is taken off the back by the
        # stand-down and never off the front.
        cost = cost_points(profile, now, target)
        fits = soc is not None and cost <= budget and now >= guard
        # The latest-release clock is a BACKSTOP now, not a ceiling (2026-09-23).
        # It was the rule while the night was settled in watt-hours and the
        # arithmetic could not be trusted to let go; the landing rule aims at
        # the floor directly, and forcing a release at the clock would spend
        # past it -- "nothing less, nothing more". So it only applies with no
        # SOC to compute from, which is the one case the arithmetic is blind.
        blind = soc is None
        overdue = blind and latest is not None and now >= latest
        spent = dict(trim=self.trim, budget=whole, cost=cost,
                     rate=profile.points_per_kwh,
                     stand_down=end if end < target else None,
                     need_wh=need, usable_wh=usable, turnaround_at=target,
                     request=asked)

        if self.released_night == night or fits or overdue:
            self.released_night = night
            if now >= end and end < target:
                # the back edge of the window: yesterday landed under the
                # floor, so this night stops short of dawn by that much
                return Decision(
                    SUB, "stand-down",
                    "stood down at %s - %.0f points short of the floor last "
                    "night, so the pack hands back before the sun%s"
                    % (end.strftime("%H:%M"), -self.trim, note),
                    release_at=None, **spent)
            return Decision(
                SBU, "night",
                "reserve released - the pack carries the house to the sun (~%s)%s%s"
                % (fmt_hm(profile.turnaround_min),
                   " - past the latest release %s" % latest.strftime("%H:%M")
                   if overdue and not fits else "", note),
                release_at=now, **spent)

        release = release_at_points(profile, now, soc, self.floor, self.trim,
                                    margin_points, target)
        if release is not None and release < guard:
            release = guard                    # not in the evening reserve
        if release is None or release >= target:
            release = None
        at_latest = (blind and latest is not None and latest < target
                     and (release is None or latest < release))
        if at_latest:
            release = latest
        if release is None:
            why = ("holding the reserve for an outage - too little above the "
                   "floor to release tonight")
        else:
            why = ("holding the reserve for an outage - release to the pack ~%s%s"
                   % (release.strftime("%H:%M"), " (the latest)" if at_latest else ""))
        return Decision(SUB, "hold", why + note, release_at=release, **spent)


def headline(payload: dict | None) -> str | None:
    """One short line for the tray menu and the watch screen, from the
    payload the collector publishes in state.json.

    >>> headline({"enabled": False, "want": "SUB", "phase": "hold", "release_at": "01:10"})
    'Would set SUB: reserve held, release ~01:10'
    >>> headline({"enabled": True, "want": "SBU", "phase": "night"})
    'Auto SBU: reserve released, pack to the sun'
    >>> headline({"enabled": True, "want": "SUB", "phase": "requested", "until": "05:30"})
    'Auto SUB: on request until 05:30'
    >>> headline(None) is None
    True
    """
    if not payload or not payload.get("want"):
        return None
    phase = payload.get("phase")
    if phase == "floor":
        what = "at the floor, grid until the sun"
    elif phase == "requested":
        what = "on request until %s" % (payload.get("until") or "?")
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
# the day as a rule -- the lanes the plan cuts across 24 hours
# --------------------------------------------------------------------------
#
# The watch screen carries a 24-hour rule under the board: what the house
# actually ran on (the collector's tape) and where the plan cuts from one
# setting to the other. Both sides are lanes -- a stretch of clock with one
# setting on it -- so one renderer draws them and the estimate lines up
# with the record.
#
# The rule's 24 hours run **sunrise to sunrise**, not midnight to midnight
# (owner, 2026-09-22): the window opens at the turnaround, the moment the
# sun starts lifting the pack, and closes at the next one. That is the
# plan's own day -- both cuts, dusk and the release, fall inside it, and
# the night is one stretch instead of being sawn in half at midnight.
#
# Minutes are always counted from TODAY's midnight, so they run negative
# into yesterday and past 1440 into tomorrow: a release at 01:20 decided at
# 22:00 is 1520, and a mark from yesterday evening is -120. `window_origin`
# gives the left edge; the renderer maps a minute to x by subtracting it.

FULL_DAY = 24 * 60

# How far the "SUB at the floor" lane is drawn before it fades out. That
# hold ends when the sun has lifted the pack past the resume level, and
# nothing can say when: the lane states the setting and then admits it.
FLOOR_FADE_MIN = 120


def _show(lanes) -> list:
    """Doctest helper: lanes as (from, to, want, sure) tuples."""
    return [(l["from"], l["to"], l["want"], l["sure"]) for l in lanes]


def _lane(start, end, want: str, sure: bool, open_end: bool = False) -> dict:
    return {"from": int(start), "to": int(end), "want": want,
            "sure": bool(sure), "open": bool(open_end)}


def window_origin(turnaround_min, now_min) -> int:
    """The left edge of the rule: the last turnaround at or before now --
    the sun's own midnight. Minutes from today's midnight, negative when
    that sunrise was yesterday's.

    >>> window_origin(434, 20 * 60)          # this morning's 07:14
    434
    >>> window_origin(434, 3 * 60)           # before dawn: yesterday's
    -1006
    """
    origin = int(turnaround_min)
    while origin > now_min:
        origin -= FULL_DAY
    return origin


def _next_release(after: int, release_min=None, latest_min=None):
    """The next release at or after `after`, and whether it is the plan's
    own arithmetic (True) or the latest-release clock standing in (False).

    >>> _next_release(20 * 60, 23 * 60 + 30, 2 * 60)
    (1410, True)
    >>> _next_release(14 * 60, None, 2 * 60)           # tomorrow's 02:00
    (1560, False)
    >>> _next_release(14 * 60, None, None)
    (None, False)
    """
    for value, sure in ((release_min, True), (latest_min, False)):
        if value is None:
            continue
        minute = int(value)
        while minute < after:
            minute += FULL_DAY
        return minute, sure
    return None, False


def lanes_ahead(now_min: int, dusk_min: int, release_min=None, latest_min=None,
                want: str = "SBU", phase: str = "day",
                horizon_min: int = FULL_DAY) -> list[dict]:
    """The plan from `now_min` to `horizon_min` -- the right edge of the
    rule, which is the next sunrise, or midnight if nothing says otherwise.

    Two cuts a day: SBU -> SUB at dusk, when the pack becomes the outage
    reserve, and SUB -> SBU at the release, when what is above the floor
    can carry the house to the turnaround. The release is only computed
    once the hold has begun; before that the latest-release clock stands
    in and the lane says so (`sure` False). The floor hold has no clock at
    all and is marked `open`.

    A day that has not reached dusk: the pack carries it, then the hold::

        >>> _show(lanes_ahead(14 * 60, 18 * 60 + 10, latest_min=2 * 60))
        [(840, 1090, 'SBU', True), (1090, 1560, 'SUB', False)]

    Inside tonight's hold, with the release worked out::

        >>> _show(lanes_ahead(20 * 60, 18 * 60 + 10, 23 * 60 + 30, 2 * 60,
        ...                  want="SUB", phase="hold"))
        [(1200, 1410, 'SUB', True), (1410, 1440, 'SBU', True)]

    Before dawn, still holding: the hold ends this morning, the day is the
    pack's, and tonight's hold is only the clock again::

        >>> _show(lanes_ahead(60, 18 * 60 + 10, 80, 2 * 60, want="SUB", phase="hold"))
        [(60, 80, 'SUB', True), (80, 1090, 'SBU', True), (1090, 1560, 'SUB', False)]

    Released, so the rest of the night is the pack's::

        >>> _show(lanes_ahead(23 * 60, 18 * 60 + 10, want="SBU", phase="night"))
        [(1380, 1440, 'SBU', True)]

    At the floor in the morning: SUB now, fading, then the day as usual::

        >>> _show(lanes_ahead(8 * 60, 18 * 60 + 10, latest_min=2 * 60,
        ...                  want="SUB", phase="floor"))
        [(480, 600, 'SUB', False), (600, 1090, 'SBU', True), (1090, 1560, 'SUB', False)]
        >>> lanes_ahead(8 * 60, 18 * 60 + 10, want="SUB", phase="floor")[0]["open"]
        True

    Run to the next sunrise instead of midnight and the whole night fits,
    both cuts in view -- which is why the rule is drawn that way::

        >>> _show(lanes_ahead(14 * 60, 18 * 60 + 10, latest_min=2 * 60,
        ...                   horizon_min=31 * 60 + 14))
        [(840, 1090, 'SBU', True), (1090, 1560, 'SUB', False), (1560, 1874, 'SBU', False)]
        >>> _show(lanes_ahead(22 * 60 + 37, 18 * 60 + 10, 2 * 60, 2 * 60,
        ...                   want="SUB", phase="hold", horizon_min=31 * 60 + 14))
        [(1357, 1560, 'SUB', True), (1560, 1874, 'SBU', True)]
    """
    lanes: list[dict] = []
    t = int(now_min)
    horizon = int(horizon_min)
    released = phase == "night"        # tonight's reserve is already let go
    sure = released
    if want == "SUB" and phase == "floor":
        end = min(t + FLOOR_FADE_MIN, dusk_min, horizon) if t < dusk_min else horizon
        if end > t:
            lanes.append(_lane(t, end, "SUB", False, open_end=True))
            t = end
    elif want == "SUB":
        release, sure = _next_release(t, release_min, latest_min)
        end = horizon if release is None else release
        if end > t:
            lanes.append(_lane(t, end, "SUB", sure))
            t, released = end, True
    if t < dusk_min:
        # Clipped at the horizon: the window closes at the next sunrise, and
        # the dusk beyond it belongs to the next day's rule, not this one.
        # Unclipped, the rule labelled its right edge with tomorrow's dusk
        # where the turnaround belongs (2026-09-23).
        lanes.append(_lane(t, min(dusk_min, horizon), "SBU", True))
        t, released = dusk_min, False
    if t < horizon:
        if released:
            lanes.append(_lane(t, horizon, "SBU", sure))
        else:
            # Tonight's hold. Its release is not worked out until the hold
            # itself begins, so here it is the clock or nothing.
            release, _ = _next_release(max(t, dusk_min), None, latest_min)
            lanes.append(_lane(t, horizon if release is None else release, "SUB", False))
            if release is not None and release < horizon:
                lanes.append(_lane(release, horizon, "SBU", False))
    return lanes


def lanes_from(payload: dict | None, now_min: int, horizon_min=None) -> list[dict]:
    """The published payload -> the lanes ahead, to the next sunrise.
    Nothing without a dusk; the midnight edge when there is no turnaround.

    >>> _show(lanes_from({"dusk": "18:10", "turnaround": "07:14",
    ...                   "latest_release": "02:00", "want": "SBU",
    ...                   "phase": "day"}, 14 * 60))
    [(840, 1090, 'SBU', True), (1090, 1560, 'SUB', False), (1560, 1874, 'SBU', False)]
    >>> _show(lanes_from({"dusk": "18:10", "latest_release": "02:00",
    ...                  "want": "SBU", "phase": "day"}, 14 * 60))
    [(840, 1090, 'SBU', True), (1090, 1560, 'SUB', False)]
    >>> lanes_from(None, 0), lanes_from({}, 0)
    ([], [])
    """
    payload = payload or {}
    dusk = parse_hm(payload.get("dusk"))
    if dusk is None:
        return []
    if horizon_min is None:
        turnaround = parse_hm(payload.get("turnaround"))
        horizon_min = (window_origin(turnaround, now_min) + FULL_DAY
                       if turnaround is not None else FULL_DAY)
    return lanes_ahead(now_min, dusk,
                       release_min=parse_hm(payload.get("release_at")),
                       latest_min=parse_hm(payload.get("latest_release")),
                       want=payload.get("want") or "SBU",
                       phase=payload.get("phase") or "day",
                       horizon_min=horizon_min)


def _mark_min(at, today=None):
    """A tape mark's time as minutes from today's midnight. 'HH:MM' is
    taken as today; a dated mark counts a day per day, so yesterday
    evening goes negative -- the rule's night crosses midnight and the
    two halves have to land on one scale.

    >>> _mark_min("07:30")
    450
    >>> _mark_min("2026-09-21T22:00", dt.date(2026, 9, 22))
    -120
    >>> _mark_min("2026-09-22T07:30", dt.date(2026, 9, 22))
    450
    >>> _mark_min("nonsense"), _mark_min(None), _mark_min("x T", dt.date(2026, 9, 22))
    (None, None, None)
    """
    if not isinstance(at, str):
        return None
    if "T" not in at:
        return parse_hm(at)
    date, _, hm = at.partition("T")
    minute = parse_hm(hm)
    if minute is None or today is None:
        return minute
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        return None
    return minute + (day - today).days * FULL_DAY


def lanes_behind(marks, now_min: int, today=None, since=None) -> list[dict]:
    """The collector's tape -> what the house actually ran on, from the
    first mark to now. Each lane carries the plan that was in force over
    it as well, so a stretch the inverter spent disagreeing can be marked.
    `since` is the rule's left edge: anything older is off the scale. Each
    lane also carries whether the grid was there, so a SUB stretch the pack
    ended up carrying -- an outage -- can be told from the plan working.

    >>> def _behind(*a, **k):
    ...     return [(l["from"], l["to"], l["want"], l["actual"]) for l in lanes_behind(*a, **k)]
    >>> _behind([{"at": "00:00", "want": "SBU", "current": "SUB"},
    ...          {"at": "07:30", "want": "SBU", "current": "SBU"}], 600)
    [(0, 450, 'SBU', 'SUB'), (450, 600, 'SBU', 'SBU')]
    >>> _behind([{"at": "09:00", "want": "SBU", "current": None}], 600)
    [(540, 600, 'SBU', None)]
    >>> lanes_behind(None, 600), lanes_behind([{"at": "bad"}], 600)
    ([], [])

    Yesterday's marks land where they belong, and the window's left edge
    trims what is off the scale::

        >>> _behind([{"at": "2026-09-21T17:00", "want": "SUB", "current": "SUB"},
        ...          {"at": "2026-09-22T02:00", "want": "SBU", "current": "SBU"}],
        ...         3 * 60, dt.date(2026, 9, 22), since=-6 * 60)
        [(-360, 120, 'SUB', 'SUB'), (120, 180, 'SBU', 'SBU')]

    The grid rides along::

        >>> [(l["actual"], l["grid"]) for l in lanes_behind(
        ...     [{"at": "20:00", "want": "SUB", "current": "SUB", "grid": True},
        ...      {"at": "21:00", "want": "SUB", "current": "SUB", "grid": False}], 22 * 60)]
        [('SUB', True), ('SUB', False)]
    """
    out: list[dict] = []
    for mark in marks or []:
        if not isinstance(mark, dict):
            continue
        start = _mark_min(mark.get("at"), today)
        if start is None or start > now_min:
            continue
        if out:
            out[-1]["to"] = start
        out.append({"from": start, "to": int(now_min), "want": mark.get("want"),
                    "actual": mark.get("current"), "grid": mark.get("grid"),
                    "src": mark.get("src")})
    if since is not None:
        out = [lane for lane in out if lane["to"] > since]
        if out:
            out[0]["from"] = max(out[0]["from"], int(since))
    return [lane for lane in out if lane["to"] > lane["from"]]


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
