"""Internet time -> inverter clock.

Goal #1 of the project. Three independent paths exist, in descending order of
how much we trust them:

  1. SNTP -> local log timestamps.  Always works, needs no hardware
     cooperation, and is what actually makes the data trustworthy.
  2. SNTP -> FC=1 heartbeat reply.  The Eybond collector asks the server for
     the time on every keepalive; some firmwares push that straight into the
     inverter RTC. Free to try -- we have to answer the heartbeat anyway.
  3. SNTP -> `DAT<YYMMDDHHMMSS>` over PI30.  Absent from the published Axpert
     spec and widely reported to return ACK while the RTC never moves. We send
     it, then read the clock back and *verify*, rather than believing the ACK.

Path 3 is the only one that can be proven or disproven, and `verify_sync()`
is what does it: write, read back, compare. That turns "documented to lie"
into a measurement on this specific unit.

Stdlib only -- the SNTP client below is ~30 lines rather than a dependency.
"""
from __future__ import annotations

import datetime as dt
import socket
import struct

# 1900-01-01 -> 1970-01-01, the NTP/Unix epoch gap
NTP_EPOCH_DELTA = 2208988800


class ClockError(RuntimeError):
    pass


def sntp_query(host: str, timeout: float = 3.0) -> dt.datetime:
    """One SNTP round trip. Returns an aware UTC datetime.

    Mode 3 (client), version 4, everything else zero -- the minimal valid
    request. We read the transmit timestamp and correct for half the round
    trip, which is as good as this needs to be.
    """
    request = b"\x1b" + 47 * b"\0"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        t0 = dt.datetime.now(dt.timezone.utc)
        sock.sendto(request, (host, 123))
        data, _addr = sock.recvfrom(96)
        t3 = dt.datetime.now(dt.timezone.utc)
    except OSError as exc:
        raise ClockError(f"{host}: {exc}") from exc
    finally:
        sock.close()

    if len(data) < 48:
        raise ClockError(f"{host}: short reply ({len(data)} B)")
    seconds, fraction = struct.unpack("!II", data[40:48])
    if seconds == 0:
        raise ClockError(f"{host}: null transmit timestamp")
    server = dt.datetime.fromtimestamp(
        seconds - NTP_EPOCH_DELTA + fraction / 2**32, dt.timezone.utc)
    return server + (t3 - t0) / 2


def internet_utc(hosts, timeout: float = 3.0) -> tuple[dt.datetime, str]:
    """First server that answers wins. Returns (utc_datetime, host_used)."""
    errors = []
    for host in hosts:
        try:
            return sntp_query(host, timeout), host
        except ClockError as exc:
            errors.append(str(exc))
    raise ClockError("no NTP server answered: " + "; ".join(errors))


def site_tzinfo(tz_name: str | None, utc_offset_hours: float) -> dt.tzinfo:
    """Site timezone, with a fixed-offset fallback.

    zoneinfo needs the tzdata package on some Windows Pythons. Pakistan has no
    DST, so a fixed +05:00 is exactly equivalent and always available.
    """
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return dt.timezone(dt.timedelta(hours=utc_offset_hours))


def dat_command(when: dt.datetime) -> str:
    """`DAT<YYMMDDHHMMSS>` for a local-time datetime.

    >>> dat_command(dt.datetime(2026, 9, 19, 21, 45, 3))
    'DAT260919214503'
    """
    return "DAT" + when.strftime("%y%m%d%H%M%S")


def heartbeat_time_payload(when: dt.datetime, fmt: str = "bin") -> bytes:
    """The 6-byte time stamp handed back on an FC=1 heartbeat.

    The encoding is unverified, hence three candidates:
      "bin"   six raw bytes: yy mm dd hh mm ss   (yy = year - 2000)
      "bcd"   the same six, packed BCD
      "ascii" 12 ASCII digits, YYMMDDHHMMSS

    >>> heartbeat_time_payload(dt.datetime(2026, 9, 19, 21, 45, 3)).hex()
    '1a0913152d03'
    >>> heartbeat_time_payload(dt.datetime(2026, 9, 19, 21, 45, 3), "bcd").hex()
    '260919214503'
    """
    parts = (when.year % 100, when.month, when.day,
             when.hour, when.minute, when.second)
    if fmt == "ascii":
        return ("%02d" * 6 % parts).encode("ascii")
    if fmt == "bcd":
        return bytes(((value // 10) << 4) | (value % 10) for value in parts)
    return bytes(parts)


def parse_inverter_time(text: str) -> dt.datetime | None:
    """Decode whatever `QT` came back with.

    Firmwares differ: 14 digits (YYYYMMDDHHMMSS) or 12 (YYMMDDHHMMSS).

    >>> parse_inverter_time("20260919214503")
    datetime.datetime(2026, 9, 19, 21, 45, 3)
    >>> parse_inverter_time("260919214503")
    datetime.datetime(2026, 9, 19, 21, 45, 3)
    >>> parse_inverter_time("NAK") is None
    True
    """
    digits = "".join(ch for ch in text if ch.isdigit())
    for length, pattern in ((14, "%Y%m%d%H%M%S"), (12, "%y%m%d%H%M%S")):
        if len(digits) == length:
            try:
                return dt.datetime.strptime(digits, pattern)
            except ValueError:
                return None
    return None


def drift_seconds(inverter: dt.datetime, reference: dt.datetime) -> float:
    """How far the inverter RTC is from the reference wall clock, in seconds.

    Both are naive local time or both aware -- callers keep them consistent.
    """
    if inverter.tzinfo is None and reference.tzinfo is not None:
        reference = reference.replace(tzinfo=None)
    return (inverter - reference).total_seconds()


def humanise_drift(seconds: float) -> str:
    """>>> humanise_drift(-3725.0)
    'inverter is 1h 2m 5s behind'
    """
    if abs(seconds) < 1:
        return "in sync (under 1s)"
    direction = "ahead" if seconds > 0 else "behind"
    total = int(abs(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    chunks = [f"{hours}h"] if hours else []
    if minutes or hours:
        chunks.append(f"{minutes}m")
    chunks.append(f"{secs}s")
    return f"inverter is {' '.join(chunks)} {direction}"


if __name__ == "__main__":
    import doctest
    print(doctest.testmod())
