#!/usr/bin/env python3
"""phantom-bridge control CLI -- every read and every write, one entry point.

    python ctl.py probe-link                 does a link come up at all
    python ctl.py status                     one power-flow panel
    python ctl.py watch                      live panel, refreshed
    python ctl.py read QPIGS QMOD            decoded reads
    python ctl.py read --all                 sweep the whole catalogue
    python ctl.py time                       inverter clock vs internet time
    python ctl.py time --sync                set it, then prove it moved
    python ctl.py controls                   list every control and its values
    python ctl.py set output-priority sbu    write one (needs ALLOW_CLOCK_WRITES)
    python ctl.py raw QPIGS                  send anything
    python ctl.py discover                   identification sweep -> FINDINGS
    python ctl.py auto                       the SUB/SBU plan: profile, verdict, release times
    python ctl.py handover --minutes 15      lend the dongle to the phone app
    python ctl.py restore                    hand the dongle back to the cloud

Connection, in order of preference:
    --direct <ip>   connect out to the dongle; changes nothing, app keeps working
    (default)       listen and redirect the dongle to us
    --dry-run       build and print frames, send nothing
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

import bridge
import config
import flow as flowmod
import link as linkmod
import pi30


# --------------------------------------------------------------------------
# connection helpers
# --------------------------------------------------------------------------

def connect(args, log):
    return bridge.open_link(direct=args.direct, dongle=args.dongle,
                            dry_run=args.dry_run,
                            redirect=not args.no_redirect,
                            wait=args.wait, log=log)


def fmt_value(value) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def print_record(record: dict) -> None:
    command = record["cmd"]
    if not record.get("ok"):
        print(f"  {command:<10} FAILED  {record.get('error')}")
        return
    if record.get("nak"):
        print(f"  {command:<10} NAK     (not supported, or refused)")
        return
    crc = "" if record.get("crc_ok", True) else "  [CRC MISMATCH]"
    data = record.get("data")
    if not data:
        print(f"  {command:<10} {record.get('text')}{crc}")
        return
    print(f"  {command:<10} {crc}".rstrip())
    for key, value in data.items():
        if key.startswith("_") or value is None:
            continue
        if isinstance(value, dict):
            on = [name for name, flag in value.items() if flag]
            if on:
                print(f"      {key:<24} {', '.join(on)}")
        elif isinstance(value, list):
            if value:
                print(f"      {key:<24} {', '.join(str(v) for v in value)}")
        else:
            print(f"      {key:<24} {fmt_value(value)}")
    if data.get("_missing"):
        print(f"      (short by {data['_missing']} fields -- field map may "
              f"not match this firmware)")
    if data.get("_extra"):
        print(f"      (unmapped extra fields: {data['_extra']})")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_probe_link(args, conn, log) -> int:
    print("\nLink is up. Asking the three questions that gate everything else.\n")
    for command in ("QPI", "QID", "QMOD"):
        print_record(bridge.read(conn, command, log))
    session = getattr(conn, "session", None)
    if session is not None:
        print(f"\n  dialect      {session.dialect}"
              f"{' (confirmed by a valid reply)' if session.dialect_confirmed else ' (still the default guess)'}")
        print(f"  heartbeats   {session.heartbeats}")
    return 0


def cmd_status(args, conn, log) -> int:
    qpiri = (bridge.read(conn, "QPIRI", log).get("data")) if not args.quick else None
    snap = bridge.snapshot(conn, log, qpiri=qpiri)
    if not snap["flow"]:
        print("No QPIGS data -- nothing to render.")
        return 1
    print()
    print(flowmod.render(snap["flow"], when=bridge.now_site(),
                         warnings=snap["warnings"]))
    return 0


def cmd_watch(args, conn, log) -> int:
    qpiri = bridge.read(conn, "QPIRI", log).get("data")
    print("\nCtrl-C to stop.\n")
    try:
        while True:
            snap = bridge.snapshot(conn, log, qpiri=qpiri)
            if snap["flow"]:
                panel = flowmod.render(snap["flow"], when=bridge.now_site(),
                                       warnings=snap["warnings"])
                print("\n" + panel, flush=True)
            interval = (config.BATTERY_POLL_INTERVAL
                        if snap["flow"].get("on_battery") else args.interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def cmd_read(args, conn, log) -> int:
    if args.all:
        commands = list(pi30.READS)
    elif args.commands:
        commands = [c.upper() for c in args.commands]
    else:
        commands = ["QMOD", "QPIGS"]
    print()
    for command in commands:
        print_record(bridge.read(conn, command, log))
        time.sleep(config.COMMAND_GAP)
    return 0


def cmd_raw(args, conn, log) -> int:
    print()
    for command in args.commands:
        print_record(bridge.read(conn, command, log))
    return 0


def cmd_time(args, conn, log) -> int:
    print()
    if args.sync:
        result = bridge.clock_sync(conn, log, force=args.force)
        verdict = result.get("verdict") or result.get("reason") or result.get("skipped")
        if verdict:
            print(f"\n  verdict: {verdict}")
        print("\n  Either way, logs/ is stamped from NTP, so the data is "
              "correctly timed regardless of what the RTC believes.")
        return 0 if result.get("ok") else 1

    status = bridge.clock_status(conn)
    log(status)
    if not status.get("ok"):
        print(f"  {status.get('error')}")
        return 1
    print(f"  internet time   {status['site_local']}  (via {status['ntp_host']})")
    print(f"  this PC is off  {status['pc_offset_s']:+.3f} s")
    if status.get("inverter_time"):
        print(f"  inverter clock  {status['inverter_time']}")
        print(f"  drift           {status['drift_human']}")
        print("\n  To set it:  python ctl.py time --sync   (needs ALLOW_CLOCK_WRITES)")
    else:
        print(f"  inverter clock  unreadable -- {status['readback']}")
        print("\n  Without a readback, a DAT write can never be proven. The "
              "logs are NTP-stamped, which is what actually matters.")
    return 0


def cmd_controls(args, conn, log) -> int:
    print("\nEvery control this build can write.")
    print(f"ALLOW_WRITES is currently {config.ALLOW_WRITES}.\n")
    for key, setter in pi30.SETTERS.items():
        marks = []
        if setter.risk == "parallel":
            marks.append("PARALLEL-SENSITIVE")
        if setter.risk == "high":
            marks.append("DESTRUCTIVE")
        if setter.unit_index:
            marks.append("per-unit")
        mark = ("  [" + ", ".join(marks) + "]") if marks else ""
        print(f"  {key:<28} {setter.doc}{mark}")
        if setter.choices:
            print(f"  {'':<28} values: {', '.join(setter.choices)}")
        elif setter.kind == "volts":
            print(f"  {'':<28} volts, {setter.lo}-{setter.hi}")
        elif setter.kind == "amps":
            print(f"  {'':<28} {setter.lo}-{setter.hi}")
        elif setter.kind == "flag":
            print(f"  {'':<28} flags: {', '.join(sorted(pi30.FLAG_LETTERS.values()))}")
    print("\n  python ctl.py set <control> <value> --yes")
    return 0


def precheck_set(args) -> int | None:
    """Every refusal `set` can make, before any link is opened.

    Returns an exit code to stop with, or None to go ahead and connect.
    cmd_set repeats the same checks; this copy exists so that a refusal
    never costs a redirect that silences WatchPower.
    """
    setter = pi30.SETTERS.get(args.control)
    if setter is None:
        print(f"No such control: {args.control}\nRun:  python ctl.py controls")
        return 2
    try:
        pi30.build_write(args.control, args.value, unit=args.unit)
    except (ValueError, KeyError) as exc:
        print(f"{exc}")
        return 2
    if not config.ALLOW_WRITES:
        print("REFUSED: ALLOW_WRITES is False in config.py (set it in config.local.py).")
        return 1
    if setter.risk == "high" and not args.i_mean_it:
        print("REFUSED: this wipes every setting on the unit. Add --i-mean-it.")
        return 1
    if (setter.risk == "parallel" and config.PARALLEL_CONFIRMED is not False
            and not (args.all_units or args.unit is not None)):
        print("REFUSED: parallel-sensitive setting on an unsettled stack. "
              "Add --unit or --all-units.")
        return 1
    if not args.yes:
        print("Not sent. Add --yes to actually write it.")
        return 1
    return None


def cmd_set(args, conn, log) -> int:
    key = args.control
    setter = pi30.SETTERS.get(key)
    if setter is None:
        print(f"No such control: {key}\nRun:  python ctl.py controls")
        return 2

    try:
        command = pi30.build_write(key, args.value, unit=args.unit)
    except (ValueError, KeyError) as exc:
        print(f"{exc}")
        return 2

    print(f"\n  control   {key}")
    print(f"  value     {args.value}")
    print(f"  command   {command}")

    if not config.ALLOW_WRITES:
        print("\n  REFUSED: ALLOW_WRITES is False in config.py.")
        print("  That gate is deliberate -- reads should be boring before "
              "anything is written.")
        return 1

    if setter.risk == "high" and not args.i_mean_it:
        print("\n  REFUSED: this wipes every setting on the unit.")
        print("  Add --i-mean-it if that is genuinely what you want.")
        return 1

    if setter.risk == "parallel" and config.PARALLEL_CONFIRMED is not False:
        if not (args.all_units or args.unit is not None):
            print("\n  REFUSED: this setting must match across a parallel stack.")
            print("  Mismatched units fight each other.")
            print("  Whether this site is one chassis or two is still open "
                  "(config.PARALLEL_CONFIRMED is None).")
            print("\n  Either settle it:   python ctl.py read QPIRI QPGS0 QPGS1")
            print("  or acknowledge it:  add --all-units")
            return 1

    if not args.yes:
        print("\n  Not sent. Add --yes to actually write it.")
        return 1

    targets = [args.unit]
    if args.all_units and setter.unit_index:
        count = args.unit_count
        targets = list(range(count))
        print(f"  writing to units 0..{count - 1}")

    failures = 0
    for unit in targets:
        command = pi30.build_write(key, args.value, unit=unit)
        record = bridge.read(conn, command, log)
        label = f"unit {unit}" if unit is not None else "unit"
        if record.get("ok") and record.get("ack"):
            print(f"  {label}: ACK")
        elif record.get("ok") and record.get("nak"):
            print(f"  {label}: NAK -- refused")
            failures += 1
        elif record.get("ok"):
            print(f"  {label}: {record.get('text')!r}")
        else:
            print(f"  {label}: {record.get('error')}")
            failures += 1

    if setter.risk == "parallel" and not setter.unit_index and args.all_units:
        print("\n  NOTE: this command has no per-unit form. If this site is "
              "genuinely two chassis, confirm the second one took the same "
              "value before walking away.")

    print("\n  Reading it back:")
    print_record(bridge.read(conn, "QPIRI", log))
    return 1 if failures else 0


def cmd_discover(args, conn, log) -> int:
    """Identification sweep. Output is meant to be pasted into FINDINGS.md."""
    print("\nIdentification sweep -- this answers the open questions in "
          "docs/FINDINGS.md.\n")
    wanted = ["QPI", "QID", "QVFW", "QVFW2", "QPIRI", "QFLAG", "QMCHGCR",
              "QMUCHGCR", "QT", "QMOD", "QPIGS", "QPIWS",
              "QPGS0", "QPGS1", "QPGS2"]
    results = {}
    for command in wanted:
        record = bridge.read(conn, command, log)
        results[command] = record
        print_record(record)
        time.sleep(config.COMMAND_GAP)

    print("\n" + "=" * 62)
    print("VERDICTS")
    print("=" * 62)

    session = getattr(conn, "session", None)
    if session is not None:
        state = "confirmed" if session.dialect_confirmed else "unconfirmed"
        print(f"\nFraming: dialect '{session.dialect}' ({state}).")

    qpiri = (results.get("QPIRI") or {}).get("data") or {}
    rated = qpiri.get("ac_out_rating_va")
    parallel_max = qpiri.get("parallel_max_units")
    if rated:
        print(f"Rated output: {rated} VA, {qpiri.get('ac_out_rating_w')} W.")
    if parallel_max:
        print(f"QPIRI says up to {parallel_max} units may be paralleled.")

    pgs1 = results.get("QPGS1") or {}
    pgs1_data = pgs1.get("data") or {}
    if pgs1.get("nak") or not pgs1.get("ok"):
        print("QPGS1 did not answer -> ONE CHASSIS. Set PARALLEL_CONFIRMED = "
              "False in config.py and PARALLEL_COMMANDS = ['QPGS0'].")
    elif pgs1_data.get("parallel_exists"):
        print("QPGS1 answered and reports a unit present -> TWO CHASSIS. Set "
              "PARALLEL_CONFIRMED = True and PARALLEL_COMMANDS = "
              "['QPGS0', 'QPGS1'].")
    else:
        print("QPGS1 answered but reports no second unit -> ONE CHASSIS "
              "(the master answers for a slot that is empty).")

    qpigs = (results.get("QPIGS") or {}).get("data") or {}
    if qpigs:
        analysis = flowmod.analyse(qpigs, mode=((results.get("QMOD") or {}).get("data") or {}).get("mode"),
                                   qpiri=qpiri)
        if analysis.get("chassis_hint"):
            print(analysis["chassis_hint"])
        if qpigs.get("_missing"):
            print(f"QPIGS returned {qpigs['_field_count']} fields, "
                  f"{qpigs['_missing']} fewer than the 21-field map -- the "
                  f"field order needs checking against this firmware.")
        elif qpigs.get("_extra"):
            print(f"QPIGS returned {qpigs['_field_count']} fields, more than "
                  f"the map: {qpigs['_extra']}")
        else:
            print(f"QPIGS returned all {qpigs['_field_count']} mapped fields.")

    qt = results.get("QT") or {}
    if qt.get("ok") and not qt.get("nak"):
        print(f"QT answered {qt.get('text')!r} -> the clock is READABLE, so a "
              f"DAT write can be verified.")
    else:
        print("QT did not answer -> the clock cannot be read back, so DAT can "
              "never be proven. Rely on NTP-stamped logs.")

    print("\nPaste the above into docs/FINDINGS.md with today's date.")
    return 0


def cmd_auto(args, conn, log) -> int:
    """The output-priority plan, from the logs and the published state.

    Opens no link: the profile is what the collector reads, and the SOC is
    the one it last published. The table at the end is the release time
    for a range of SOCs at this moment, so the rule can be sanity-checked
    against tonight before AUTO_PRIORITY is turned on.
    """
    import datetime as dt
    import json
    import autopilot
    import days
    import policy

    now = bridge.now_site()
    store = days.DayStore()
    prof = autopilot.build_for(store, now.date())
    state = bridge.read_state(max_age_s=900) or {}
    soc = state.get("soc")
    naive = now.replace(tzinfo=None)
    gov = policy.Governor(config.AUTO_SOC_FLOOR, config.AUTO_SOC_RESUME)
    decision = gov.decide(prof, naive, soc)
    published = state.get("policy") or {}

    if args.json:
        print(json.dumps({"profile": prof.as_dict(), "soc": soc,
                          "decision": {"want": decision.name, "phase": decision.phase,
                                       "reason": decision.reason,
                                       "release_at": decision.release_at.isoformat(timespec="minutes")
                                       if decision.release_at else None,
                                       "need_wh": round(decision.need_wh),
                                       "usable_wh": round(decision.usable_wh)},
                          "published": published}, indent=1))
        return 0

    switch = "ON -- the collector writes POP" if config.AUTO_PRIORITY else "OFF -- advisory only"
    print(f"\nAutomatic output priority: {switch}")
    print(f"  floor {config.AUTO_SOC_FLOOR}%  resume {config.AUTO_SOC_RESUME}%  "
          f"dwell {config.AUTO_MIN_DWELL_S}s\n")
    print(f"PROFILE  from {prof.days} finished day(s)")
    print(f"  turnaround  {policy.fmt_hm(prof.turnaround_min)}   {prof.notes.get('turnaround')}")
    print(f"  dusk        {policy.fmt_hm(prof.dusk_min)}   {prof.notes.get('dusk')}")
    print(f"  pack        {prof.pack_wh:.0f} Wh per 100% SOC   {prof.notes.get('pack_wh')}")
    print(f"  efficiency  {prof.efficiency:.2f} load Wh per battery Wh   {prof.notes.get('efficiency')}")
    if prof.notes.get("hourly_load"):
        print(f"  hourly load {prof.notes['hourly_load']}")
    night = [h for h in range(24) if not (prof.turnaround_min <= h * 60 < prof.dusk_min)]
    print("  night load  " + "  ".join(f"{h:02d}h {prof.hourly_load_w[h]:.0f}W" for h in night))

    age = state.get("age_s")
    soc_text = "--" if soc is None else f"{soc}%"
    print(f"\nNOW      {now.strftime('%H:%M')}   SOC {soc_text}"
          + (f"  (state.json {age}s old)" if age is not None else "  (no state.json)"))
    print(f"  verdict     {decision.name}  [{decision.phase}]  {decision.reason}")
    if decision.phase in ("hold", "night"):
        print(f"  need {decision.need_wh:.0f} Wh to the turnaround, "
              f"{decision.usable_wh:.0f} Wh above the floor")
    if published:
        print(f"  collector   {policy.headline(published)}  (current {published.get('current')})")
        if published.get("last_write"):
            print(f"  last write  {published['last_write']}")

    print("\nRELEASE TIME BY SOC, from now")
    for level in range(100, config.AUTO_SOC_FLOOR, -10):
        t = policy.release_time(prof, naive, level, config.AUTO_SOC_FLOOR)
        if t is None:
            when = "never"
        elif t <= naive:
            when = "now"
        elif t >= policy.next_turnaround(prof, naive):
            when = "not tonight"
        else:
            when = t.strftime("%H:%M")
        print(f"  {level:3d}%  {when}")
    return 0


def cmd_handover(args, conn, log) -> int:
    """Lend the dongle back to the vendor cloud so the Android app can read it.

    The app talks to the cloud, not the inverter, so there is no way to detect
    it opening and alternate automatically. This is the practical substitute:
    a timed loan the running collector honours and then takes back.
    """
    if args.cancel:
        bridge.cancel_handover()
        print("\n  Handover cancelled. The collector takes the link back on "
              "its next cycle.")
        return 0
    bridge.request_handover(args.minutes)
    print(f"\n  Lending the dongle to the vendor cloud for {args.minutes:g} "
          f"minutes.")
    print("  The collector restores the cloud endpoint on its next cycle and "
          "re-takes the link when the timer expires.")
    print("\n  Give the app a couple of minutes before you expect current "
          "numbers -- it reads the cloud's copy, not the inverter.")
    if bridge.read_state(max_age_s=60) is None:
        print("\n  NOTE: no collector appears to be running, so there is "
              "nothing to hand over. Start one with: python collector.py")
    return 0


def cmd_restore(args, conn, log) -> int:
    bridge.restore(args.dongle)
    return 0


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

NEEDS_LINK = {"probe-link", "status", "watch", "read", "raw", "time",
              "set", "discover"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--direct", metavar="IP",
                        help="connect out to the dongle; changes nothing on it")
    parser.add_argument("--dongle", metavar="IP",
                        help="dongle IP for the redirect path")
    parser.add_argument("--no-redirect", action="store_true",
                        help="listen only; do not touch dongle config")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and print frames, send nothing")
    parser.add_argument("--wait", type=float, default=120.0,
                        help="seconds to wait for the dongle (default 120)")
    parser.add_argument("--quiet", action="store_true",
                        help="do not echo the JSONL log to the console")

    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("probe-link", help="bring a link up and identify the unit")

    status = subs.add_parser("status", help="one power-flow panel")
    status.add_argument("--quick", action="store_true",
                        help="skip the QPIRI rating read")

    watch = subs.add_parser("watch", help="live power-flow panel")
    watch.add_argument("--interval", type=float, default=config.POLL_INTERVAL)

    read = subs.add_parser("read", help="decoded reads")
    read.add_argument("commands", nargs="*")
    read.add_argument("--all", action="store_true",
                      help="sweep the entire read catalogue")

    raw = subs.add_parser("raw", help="send arbitrary PI30 commands")
    raw.add_argument("commands", nargs="+")

    timec = subs.add_parser("time", help="inverter clock vs internet time")
    timec.add_argument("--sync", action="store_true",
                       help="set the clock, then read it back to prove it moved")
    timec.add_argument("--force", action="store_true",
                       help="write even if the drift is within tolerance")

    subs.add_parser("controls", help="list every control")

    setc = subs.add_parser("set", help="write one control")
    setc.add_argument("control")
    setc.add_argument("value", nargs="?")
    setc.add_argument("--unit", type=int, help="parallel unit index")
    setc.add_argument("--all-units", action="store_true",
                      help="apply across the parallel stack")
    setc.add_argument("--unit-count", type=int, default=2,
                      help="how many units --all-units covers (default 2)")
    setc.add_argument("--yes", action="store_true", help="actually send it")
    setc.add_argument("--i-mean-it", action="store_true",
                      help="required for destructive controls")

    subs.add_parser("discover", help="identification sweep for FINDINGS.md")

    auto = subs.add_parser("auto", help="the SUB/SBU plan from the logs; opens no link")
    auto.add_argument("--json", action="store_true", help="the numbers, as JSON")

    hand = subs.add_parser("handover",
                           help="lend the dongle to the WatchPower app for a while")
    hand.add_argument("--minutes", type=float, default=15.0)
    hand.add_argument("--cancel", action="store_true",
                      help="end the loan early and take the link back")
    subs.add_parser("restore", help="hand the dongle back to the vendor cloud")
    return parser


HANDLERS = {
    "probe-link": cmd_probe_link,
    "status": cmd_status,
    "watch": cmd_watch,
    "read": cmd_read,
    "raw": cmd_raw,
    "time": cmd_time,
    "controls": cmd_controls,
    "set": cmd_set,
    "discover": cmd_discover,
    "auto": cmd_auto,
    "handover": cmd_handover,
    "restore": cmd_restore,
}


def main() -> int:
    args = build_parser().parse_args()
    log = bridge.quiet_logger() if args.quiet else bridge.make_logger()
    handler = HANDLERS[args.command]

    if args.command not in NEEDS_LINK:
        return handler(args, None, log)

    # Writes are validated before anything is connected or redirected: being
    # told "no" should never cost a redirect that silences WatchPower.
    if args.command == "set":
        refusal = precheck_set(args)
        if refusal is not None:
            return refusal

    conn = None
    try:
        conn = connect(args, log)
        return handler(args, conn, log)
    except linkmod.LinkError as exc:
        print(f"\n{exc}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
