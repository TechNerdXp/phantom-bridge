"""Shared machinery for collector.py and ctl.py.

Connecting, logging, polling and the clock-sync routine all live here so the
two entry points cannot drift apart.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _path in (str(_ROOT), str(_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import clock
import config
import energy
import flow as flowmod
import link as linkmod
import netutil
import pi30

# From source the two agree. Frozen, __file__ is inside the bundle and only
# config knows where the checkout is (see config._project_root).
_ROOT = config.ROOT
LOG_DIR = _ROOT / "logs"


def site_tz() -> dt.tzinfo:
    return clock.site_tzinfo(config.SITE_TZ, config.SITE_UTC_OFFSET_HOURS)


def now_site() -> dt.datetime:
    return dt.datetime.now(site_tz())


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

def make_logger(echo: bool = True, to_file: bool = True):
    """A JSONL logger. One file per day, UTC-stamped, append-only."""
    if to_file:
        LOG_DIR.mkdir(exist_ok=True)

    def log(record) -> None:
        if isinstance(record, str):
            if echo:
                print(record)
            return
        record = dict(record)
        record["ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
        record["local"] = now_site().isoformat(timespec="seconds")
        if to_file:
            path = LOG_DIR / f"{dt.date.today().isoformat()}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if echo:
            print(json.dumps(record, ensure_ascii=False))

    return log


def quiet_logger(to_file: bool = True):
    return make_logger(echo=False, to_file=to_file)


# --------------------------------------------------------------------------
# connecting
# --------------------------------------------------------------------------

def session_kwargs(log):
    return {
        "dialect": config.EYBOND_DIALECT,
        "auto_dialect": config.AUTO_DIALECT,
        "tzinfo": site_tz(),
        "heartbeat_format": config.HEARTBEAT_TIME_FORMAT,
        "devcode": config.DEVCODE,
        "devaddr": config.DEVADDR,
        "log": log,
    }


def local_ip() -> str:
    return config.LOCAL_IP or netutil.primary_lan_ip() or "127.0.0.1"


def open_link(*, direct: str | None = None, dongle: str | None = None,
              dry_run: bool = False, redirect: bool = True,
              wait: float = 120.0, log=print, announce=print):
    """Get a live link by whichever route is available.

    `direct` connects out to the dongle and changes nothing on it.
    Otherwise we listen and (unless redirect is False) send the `set>server=`
    string that points the dongle at us.
    """
    if dry_run:
        announce("DRY RUN -- frames will be built and printed, nothing sent.")
        return linkmod.DryRunLink(config.EYBOND_DIALECT, log=announce)

    if direct:
        announce(f"Connecting out to {direct}:{config.DIRECT_TCP_PORT} "
                 f"(dongle config untouched)")
        conn = linkmod.DirectLink(direct, config.DIRECT_TCP_PORT,
                                  **session_kwargs(log))
        conn.connect()
        announce("Connected.")
        return conn

    host = local_ip()
    server = linkmod.ServerLink("0.0.0.0", config.LOCAL_PORT, **session_kwargs(log))
    server.start()
    announce(f"Listening on 0.0.0.0:{config.LOCAL_PORT}  (we are {host})")

    def send_redirect_once() -> None:
        target = f"{host}:{config.LOCAL_PORT}"
        announce(f"Redirecting dongle -> {target}")
        linkmod.udp_config(f"set>server={target};",
                           dongle or config.DONGLE_IP,
                           config.UDP_CONFIG_PORT,
                           netutil.broadcast_addresses(config.LAN_CIDR_BITS),
                           log=announce)

    if redirect:
        send_redirect_once()

    # One redirect is not always enough. When the previous collector died
    # without closing (a kill, a crash, a shutdown), the dongle still holds
    # that half-open session and answers `rsp>server=1;` without dropping
    # it; a second redirect a little later is what actually moves it (seen
    # 2026-09-20: 3 min of silence, then connected within 8 s of the
    # re-send). So the wait is in slices, with the redirect repeated
    # between them -- the same rule the poll loop applies to a dead link.
    announce(f"Waiting up to {wait:.0f}s for the dongle to connect ...")
    deadline = time.monotonic() + wait
    session = None
    while session is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        session = server.wait_for_session(min(config.REDIRECT_RETRY_SECONDS,
                                              remaining))
        if session is None and redirect and time.monotonic() < deadline:
            send_redirect_once()
    if session is None:
        announce("")
        announce("No connection. The usual causes, in order:")
        announce("  * Windows Firewall is dropping it. Run, elevated:")
        announce("    " + netutil.firewall_hint(config.LOCAL_PORT))
        announce("  * The dongle never got the redirect -- run probe.py to "
                 "find its IP, then pass --dongle <ip>")
        announce("  * AP/client isolation on the router")
        raise linkmod.LinkError("dongle did not connect")
    announce("Dongle connected.")
    return server


def send_redirect(dongle: str | None = None, announce=print) -> None:
    """Re-send the `set>server=` string without rebuilding the listener.

    Used to recover a resident collector: if the dongle has gone quiet for a
    while it may have lost the setting (or been power-cycled), and re-sending
    is cheap next to sitting there logging failures forever.
    """
    host = local_ip()
    linkmod.udp_config(f"set>server={host}:{config.LOCAL_PORT};",
                       dongle or config.DONGLE_IP,
                       config.UDP_CONFIG_PORT,
                       netutil.broadcast_addresses(config.LAN_CIDR_BITS),
                       log=announce)


def restore(dongle: str | None = None, announce=print) -> None:
    """Point the dongle back at its vendor cloud."""
    announce("Restoring the vendor cloud endpoint ...")
    linkmod.udp_config("set>server=default;",
                       dongle or config.DONGLE_IP,
                       config.UDP_CONFIG_PORT,
                       netutil.broadcast_addresses(config.LAN_CIDR_BITS),
                       log=announce)
    announce("Check WatchPower -- the module should return to Online.")


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def read(conn, command: str, log=None, timeout: float | None = None) -> dict:
    """Send one command and decode the reply into a record."""
    timeout = timeout or config.COMMAND_TIMEOUT
    record: dict = {"event": "read", "cmd": command}
    try:
        reply = conn.request(command, timeout)
    except linkmod.LinkError as exc:
        record.update(ok=False, error=str(exc))
    else:
        record.update(ok=True, text=reply.text, crc_ok=reply.crc_ok,
                      nak=reply.is_nak, ack=reply.is_ack)
        if not reply.is_nak and not reply.is_ack:
            record["data"] = pi30.decode(command, reply.text)
    if log:
        log(record)
    return record


def read_many(conn, commands, log=None, gap: float | None = None) -> dict:
    """Run a list of commands, returning {command: record}."""
    gap = config.COMMAND_GAP if gap is None else gap
    out: dict = {}
    for index, command in enumerate(commands):
        out[command] = read(conn, command, log)
        if gap and index < len(commands) - 1:
            time.sleep(gap)
    return out


def snapshot(conn, log=None, qpiri: dict | None = None) -> dict:
    """One QMOD + QPIGS + QPIWS cycle, analysed into a power-flow picture."""
    commands = ["QMOD", "QPIGS", "QPIWS"] + list(config.PARALLEL_COMMANDS)
    records = read_many(conn, commands, log)

    qpigs = (records.get("QPIGS") or {}).get("data") or {}
    mode = ((records.get("QMOD") or {}).get("data") or {}).get("mode")
    warnings = (records.get("QPIWS") or {}).get("data") or {}

    analysis = flowmod.analyse(
        qpigs, mode=mode, qpiri=qpiri,
        trust_soc=config.TRUST_SOC,
        capacity_ah=config.BATTERY_CAPACITY_AH,
        usable_fraction=config.BATTERY_USABLE_FRACTION,
        grid_present_volts=config.GRID_PRESENT_VOLTS,
    ) if qpigs else {}

    # The one-chassis-or-two question is settled in config; once it is, the
    # hint is noise on every single panel.
    if config.PARALLEL_CONFIRMED is not None:
        analysis.pop("chassis_hint", None)

    return {"records": records, "flow": analysis, "warnings": warnings,
            "qpigs": qpigs, "mode": mode}


# --------------------------------------------------------------------------
# energy counters
# --------------------------------------------------------------------------

ENERGY_PATH = LOG_DIR / "energy.json"


def read_energy_day(conn, day: dt.date, log=None) -> tuple[int | None, int | None]:
    """The inverter's own PV and load watt-hours for one day: (pv_wh, load_wh).

    Either is None if the read failed or NAKed. A day the inverter has no
    record of comes back as 0, which is a real answer, not a failure.
    """
    out = []
    for kind in ("pv", "load"):
        record = read(conn, pi30.energy_command(kind, day), log)
        data = record.get("data") or {}
        out.append(data.get("wh") if record.get("ok") and not record.get("nak") else None)
        time.sleep(config.COMMAND_GAP)
    return out[0], out[1]


def refresh_energy(conn, book: energy.DayBook, today: dt.date, log=None,
                   history_days: int = 0) -> None:
    """Pull today's counters, and optionally back-fill the days before.

    History is only fetched for days that have no counter yet, plus
    yesterday, whose stored value may be from before midnight.
    """
    pv, load = read_energy_day(conn, today, log)
    book.set_counters(today, pv_wh=pv, load_wh=load)
    for back in range(1, history_days + 1):
        day = today - dt.timedelta(days=back)
        rec = book.days.get(day.isoformat()) or {}
        if back > 1 and rec.get("pv_wh") is not None and rec.get("load_wh") is not None:
            continue
        pv, load = read_energy_day(conn, day, log)
        if pv is None and load is None:
            break                      # the unit has stopped answering; stop asking
        book.set_counters(day, pv_wh=pv, load_wh=load)


# --------------------------------------------------------------------------
# clock -- goal #1
# --------------------------------------------------------------------------

def read_inverter_time(conn) -> tuple[dt.datetime | None, str]:
    """Best effort read of the RTC. Returns (naive local datetime, note)."""
    try:
        reply = conn.request("QT", config.COMMAND_TIMEOUT)
    except linkmod.LinkError as exc:
        return None, f"QT did not answer ({exc})"
    if reply.is_nak:
        return None, "QT returned NAK -- this firmware cannot report its clock"
    parsed = clock.parse_inverter_time(reply.text)
    if parsed is None:
        return None, f"QT returned {reply.text!r}, which is not a timestamp"
    return parsed, "QT answered"


def clock_status(conn) -> dict:
    """Compare the inverter RTC against internet time. Reads only."""
    out: dict = {"event": "clock-status"}
    try:
        utc, host = clock.internet_utc(config.NTP_SERVERS)
    except clock.ClockError as exc:
        out.update(ok=False, error=str(exc))
        return out
    local = utc.astimezone(site_tz())
    out.update(ntp_host=host, internet_utc=utc.isoformat(timespec="seconds"),
               site_local=local.isoformat(timespec="seconds"),
               pc_offset_s=round(
                   (dt.datetime.now(dt.timezone.utc) - utc).total_seconds(), 3))

    inverter, note = read_inverter_time(conn)
    out["readback"] = note
    if inverter is None:
        out.update(ok=True, inverter_time=None, drift_s=None)
        return out
    drift = clock.drift_seconds(inverter, local.replace(tzinfo=None))
    out.update(ok=True, inverter_time=inverter.isoformat(sep=" "),
               drift_s=round(drift, 1), drift_human=clock.humanise_drift(drift))
    return out


def clock_sync(conn, log=None, announce=print, force: bool = False) -> dict:
    """Set the RTC from internet time, then read it back and check.

    The point of the readback is that `DAT` is widely reported to answer ACK
    while the clock never moves. An ACK is not evidence; a changed QT is.
    """
    result: dict = {"event": "clock-sync"}

    if not (config.ALLOW_CLOCK_WRITES or config.ALLOW_WRITES):
        result.update(ok=False, skipped="ALLOW_CLOCK_WRITES is False in config.py")
        announce("Clock sync skipped: ALLOW_CLOCK_WRITES is False in config.py.")
        if log:
            log(result)
        return result

    try:
        utc, host = clock.internet_utc(config.NTP_SERVERS)
        anchor = time.monotonic()
    except clock.ClockError as exc:
        result.update(ok=False, error=str(exc))
        announce(f"No internet time: {exc}")
        if log:
            log(result)
        return result

    local = utc.astimezone(site_tz())
    result.update(ntp_host=host, target=local.isoformat(timespec="seconds"))
    announce(f"Internet time via {host}: {local.isoformat(timespec='seconds')}")

    before, note = read_inverter_time(conn)
    result["before"] = before.isoformat(sep=" ") if before else None
    result["readback_supported"] = before is not None
    announce(f"  before: {before if before else note}")

    if before is not None and not force:
        drift = clock.drift_seconds(before, local.replace(tzinfo=None))
        result["drift_before_s"] = round(drift, 1)
        if abs(drift) <= config.CLOCK_MAX_DRIFT_SECONDS:
            result.update(ok=True, action="none",
                          reason=f"drift {drift:.0f}s within the "
                                 f"{config.CLOCK_MAX_DRIFT_SECONDS}s tolerance")
            announce(f"  {clock.humanise_drift(drift)} -- inside tolerance, "
                     f"leaving it alone")
            if log:
                log(result)
            return result
        announce(f"  {clock.humanise_drift(drift)}")

    # Carry the NTP reading forward by however long the readback above took,
    # rather than sending a timestamp that is already seconds stale. The PC
    # clock is not used for this -- it was measured off NTP itself.
    elapsed = dt.timedelta(seconds=time.monotonic() - anchor)
    command = clock.dat_command((utc + elapsed).astimezone(site_tz()))
    result["command"] = command
    announce(f"  sending {command}")
    try:
        reply = conn.request(command, config.COMMAND_TIMEOUT)
    except linkmod.LinkError as exc:
        result.update(ok=False, error=str(exc))
        announce(f"  no reply: {exc}")
        if log:
            log(result)
        return result

    result["reply"] = reply.text
    if reply.is_nak:
        result.update(ok=False, action="nak",
                      verdict="DAT rejected -- this firmware has no settable clock")
        announce("  NAK. Expected; local NTP-stamped logs are the real answer.")
        if log:
            log(result)
        return result

    announce(f"  reply: {reply.text!r}")
    time.sleep(1.5)
    after, after_note = read_inverter_time(conn)
    result["after"] = after.isoformat(sep=" ") if after else None

    if after is None:
        result.update(ok=True, action="ack-unverified",
                      verdict=f"DAT was accepted but {after_note} -- cannot "
                              f"prove the clock moved")
        announce("  Accepted, but there is no readback to prove it took.")
    else:
        reference = (utc + dt.timedelta(seconds=time.monotonic() - anchor)
                     ).astimezone(site_tz()).replace(tzinfo=None)
        drift = clock.drift_seconds(after, reference)
        result["drift_after_s"] = round(drift, 1)
        if abs(drift) <= max(5, config.CLOCK_MAX_DRIFT_SECONDS):
            result.update(ok=True, action="set",
                          verdict=f"clock moved and now reads within "
                                  f"{abs(drift):.0f}s of internet time")
            announce(f"  CONFIRMED: {clock.humanise_drift(drift)} -- synced.")
        else:
            result.update(ok=False, action="ack-ignored",
                          verdict="DAT answered ACK but the clock did not move "
                                  "-- the documented failure mode")
            announce(f"  ACK but no movement ({clock.humanise_drift(drift)}). "
                     f"This is the documented failure. Stop chasing it -- the "
                     f"logs are NTP-stamped regardless.")
    if log:
        log(result)
    return result


# --------------------------------------------------------------------------
# published state -- how the tray sees the inverter without a second session
# --------------------------------------------------------------------------

STATE_PATH = LOG_DIR / "state.json"


def write_state(snap: dict, source: str = "collector", today: dict | None = None) -> None:
    """Publish the latest snapshot for other processes to read.

    Written to a temp file and renamed, so a reader never sees half a file.
    This is what keeps the tray free: it costs no extra RS-485 traffic and no
    second dongle session, which the dongle would not give us anyway.
    """
    LOG_DIR.mkdir(exist_ok=True)
    payload = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "monotonic": time.time(),
        "local": now_site().isoformat(timespec="seconds"),
        "source": source,
        "mode": snap.get("mode"),
        "flow": snap.get("flow") or {},
        "warnings": (snap.get("warnings") or {}).get("active") or [],
        "soc": (snap.get("qpigs") or {}).get("batt_soc"),
        "trust_soc": config.TRUST_SOC,
        "today": today or {},
    }
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def read_state(max_age_s: float = 120.0) -> dict | None:
    """The last published snapshot, or None if missing or stale."""
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    age = time.time() - float(payload.get("monotonic") or 0)
    if age > max_age_s:
        payload["stale"] = True
        payload["age_s"] = round(age)
        return payload
    payload["stale"] = False
    payload["age_s"] = round(age)
    return payload


# --------------------------------------------------------------------------
# handover -- lending the dongle back to the vendor cloud for a while
# --------------------------------------------------------------------------
#
# The WatchPower phone app does not talk to the dongle; it reads the vendor
# cloud, which the dongle uploads to. So there is nothing we can detect when
# the app is opened, and no way to alternate automatically on it.
#
# What works instead is a timed loan: restore the cloud endpoint, stop polling,
# and re-take the link when the timer runs out. Ask for it from the tray menu
# or `ctl.py handover`, open the app, and it comes back on its own.
#
# Expect the app to need a minute or two of cloud uploads before it shows
# current data -- it is reading the cloud's copy, not the inverter.

HANDOVER_PATH = LOG_DIR / "handover.json"


def request_handover(minutes: float) -> float:
    """Ask the running collector to hand the dongle back for `minutes`."""
    LOG_DIR.mkdir(exist_ok=True)
    until = time.time() + minutes * 60
    tmp = HANDOVER_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"until": until, "minutes": minutes}),
                   encoding="utf-8")
    tmp.replace(HANDOVER_PATH)
    return until


def handover_remaining() -> float:
    """Seconds of loan left, or 0 if none is active."""
    try:
        payload = json.loads(HANDOVER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    return max(0.0, float(payload.get("until") or 0) - time.time())


def cancel_handover() -> None:
    try:
        HANDOVER_PATH.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# autostart -- one implementation, used by both the collector and the tray
# --------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command(script: str, args=(), windowed: bool = True) -> str:
    """The command line that starts one of our entry points.

    `windowed` picks pythonw.exe, which runs with no console window -- right
    for anything resident, wrong for anything whose output you want to read.

    Frozen, the command is the exe itself with the script's name as its
    task: `PhantomBridge.exe collector --quiet`. main.py dispatches it.
    """
    if getattr(sys, "frozen", False):
        parts = ['"%s"' % sys.executable, pathlib.Path(script).stem]
        return " ".join(parts + list(args))
    launcher = pathlib.Path(sys.executable)
    if windowed:
        candidate = launcher.with_name("pythonw.exe")
        if candidate.exists():
            launcher = candidate
    target = _ROOT / script
    parts = ['"%s"' % launcher, '"%s"' % target] + list(args)
    return " ".join(parts)


def autostart_command(value_name: str) -> str | None:
    """The Run value as written, or None when autostart is off."""
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
        return str(value)
    except OSError:
        return None


def autostart_enabled(value_name: str) -> bool:
    return autostart_command(value_name) is not None


def _runs_frozen_exe(command: str) -> bool:
    """Does this Run value start with a PhantomBridge exe that still exists?"""
    head = command.split('"')[1] if command.startswith('"') else command.split(" ")[0]
    return head.lower().endswith(".exe") and pathlib.Path(head).exists()


def set_autostart(value_name: str, enable: bool, script: str, args=(),
                  windowed: bool = True) -> None:
    if sys.platform != "win32":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ,
                              launch_command(script, args, windowed))
        else:
            try:
                winreg.DeleteValue(key, value_name)
            except OSError:
                pass


def refresh_autostart(value_name: str, script: str, args=(),
                      windowed: bool = True) -> None:
    """Rewrite the autostart value if it exists, so a moved folder self-heals.

    The trap RouterOps hit: `autostart_enabled()` only asks whether the value
    exists, so a stale path still shows a tick in the menu and the only symptom
    is nothing starting after a reboot.

    One exception: a source run (`python tray.py` for a debug session) does
    not demote an entry that points at the built exe. The exe is the
    installation once it exists; the sources are for working on it.
    """
    current = autostart_command(value_name)
    if current is None:
        return
    if not getattr(sys, "frozen", False) and _runs_frozen_exe(current):
        return
    set_autostart(value_name, True, script, args, windowed)
