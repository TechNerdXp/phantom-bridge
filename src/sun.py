"""Sunrise and sunset for the site, from the date and a position.

The one anchor in this project that no measurement can drift: the sun is
where it is whatever the panels are doing. Everything else the policy reads
about the day -- the turnaround, dusk -- comes out of the logs and therefore
moves with dust on the glass, cloud, and what the house happened to be
drawing. Those readings are hooked to this so they cannot wander off on a
run of odd days (owner, 2026-09-23).

The NOAA sunrise equation, which is good to about a minute -- far inside
anything that matters here, where the figures it guards are quantised to
the minute and the hour. Pure, stdlib, doctested.
"""
from __future__ import annotations

import datetime as dt
import math

# The centre of the sun is this far below the horizon at the moment we call
# sunrise: half its disc (0.833 = 16' radius + 34' refraction).
ZENITH_OFFSET_DEG = -0.833

OBLIQUITY_DEG = 23.4397          # the tilt of the earth's axis


def _julian_day(day: dt.date) -> float:
    """Julian day number at noon UT.

    >>> _julian_day(dt.date(2000, 1, 1))
    2451545.0
    >>> _julian_day(dt.date(2026, 9, 22))
    2461306.0
    """
    a = (14 - day.month) // 12
    y = day.year + 4800 - a
    m = day.month + 12 * a - 3
    jdn = (day.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100
           + y // 400 - 32045)
    return float(jdn)


def _event(day: dt.date, lat: float, lon: float, rising: bool):
    """The Julian date of sunrise or sunset, or None inside a polar day
    or night where the sun does not cross the horizon at all."""
    n = _julian_day(day) - 2451545.0 + 0.0008
    mean_noon = n - lon / 360.0
    anomaly = math.radians((357.5291 + 0.98560028 * mean_noon) % 360.0)
    centre = (1.9148 * math.sin(anomaly) + 0.0200 * math.sin(2 * anomaly)
              + 0.0003 * math.sin(3 * anomaly))
    ecliptic = math.radians((math.degrees(anomaly) + centre + 180 + 102.9372) % 360.0)
    transit = (2451545.0 + mean_noon + 0.0053 * math.sin(anomaly)
               - 0.0069 * math.sin(2 * ecliptic))
    declination = math.asin(math.sin(ecliptic) * math.sin(math.radians(OBLIQUITY_DEG)))
    phi = math.radians(lat)
    cos_hour = ((math.sin(math.radians(ZENITH_OFFSET_DEG))
                 - math.sin(phi) * math.sin(declination))
                / (math.cos(phi) * math.cos(declination)))
    if not -1.0 <= cos_hour <= 1.0:
        return None
    hour_angle = math.degrees(math.acos(cos_hour))
    return transit + (hour_angle if not rising else -hour_angle) / 360.0


def _local_minutes(julian, day: dt.date, tz_offset_h: float):
    """A Julian date -> minutes after local midnight on `day`, or None."""
    if julian is None:
        return None
    seconds = (julian - _julian_day(day) + 0.5) * 86400.0 + tz_offset_h * 3600.0
    return seconds / 60.0


def sunrise_min(day: dt.date, lat: float, lon: float, tz_offset_h: float):
    """Sunrise as minutes after local midnight. None in a polar night.

    At an equinox on the equator the sun rises a few minutes BEFORE six,
    local solar time: refraction and the disc's own radius lift it over
    the horizon early, which is the 0.833 above.

    >>> round(sunrise_min(dt.date(2026, 3, 20), 0.0, 0.0, 0))     # 06:05
    365

    The site (Karachi, UTC+5) on a day this project has logs for, against
    the published 06:22 -- and the drift that makes it a safe anchor: under
    half a minute a day, so a reading that jumps more than a few minutes
    did not move because the sun did.

    >>> from_ = sunrise_min(dt.date(2026, 9, 22), 24.8607, 67.0011, 5)
    >>> to = sunrise_min(dt.date(2026, 9, 23), 24.8607, 67.0011, 5)
    >>> round(from_), round(to), round(to - from_, 1)
    (382, 382, 0.4)
    """
    return _local_minutes(_event(day, lat, lon, rising=True), day, tz_offset_h)


def sunset_min(day: dt.date, lat: float, lon: float, tz_offset_h: float):
    """Sunset as minutes after local midnight. None in a polar day.

    >>> round(sunset_min(dt.date(2026, 9, 22), 24.8607, 67.0011, 5))   # 18:31
    1111
    >>> round(sunset_min(dt.date(2026, 9, 22), 24.8607, 67.0011, 5)
    ...       - sunrise_min(dt.date(2026, 9, 22), 24.8607, 67.0011, 5))
    729
    """
    return _local_minutes(_event(day, lat, lon, rising=False), day, tz_offset_h)


def daylight_min(day: dt.date, lat: float, lon: float, tz_offset_h: float):
    """How long the sun is up, in minutes. None when it does not set.

    An equinox is a little OVER twelve hours, not exactly twelve, for the
    same reason sunrise is early: the sun is refracted over the horizon at
    both ends of it.

    >>> round(daylight_min(dt.date(2026, 3, 20), 0.0, 0.0, 0))     # equinox
    727
    >>> daylight_min(dt.date(2026, 6, 21), 69.65, 18.96, 2) is None   # polar day
    True
    """
    rise = sunrise_min(day, lat, lon, tz_offset_h)
    fall = sunset_min(day, lat, lon, tz_offset_h)
    if rise is None or fall is None:
        return None
    return fall - rise


if __name__ == "__main__":
    import doctest
    print(doctest.testmod())
