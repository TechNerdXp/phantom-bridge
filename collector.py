#!/usr/bin/env python3
"""Step 2 -- the resident collector. Redirects the dongle here, then polls.

    python collector.py                  redirect the dongle to us, then poll
    python collector.py --direct <ip>    connect out instead; changes nothing
    python collector.py --panel          live power-flow panel, not raw JSONL
    python collector.py --restore        hand the dongle back to the cloud

What it does each cycle: QMOD, QPIGS, QPIWS and the parallel commands, decoded
into named values, appended to logs/<date>.jsonl with an NTP-correct UTC
stamp, and published to logs/state.json for the tray to read.

Cost and contention, deliberately:

  Nothing spins. The main thread parks in a kernel wait until stopped; the
  poll loop waits on an event with a timeout, so between samples the process
  is descheduled and costs nothing.

  The cadence is adaptive, not fixed: 5 s on grid, 1 s once we are actually on
  battery, which is the only time a second of resolution is worth the RS-485
  traffic. That is a deliberate choice against WatchPower's own rate, not a
  default.

  The link is claimed with a named mutex for the whole run. Only one process
  talks to the dongle -- the tray yields to this one and reads the state file
  instead of opening a competing session.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

import arbiter
import autopilot
import bridge
import config
import energy
import flow as flowmod
import link as linkmod

AUTOSTART_VALUE = "PhantomBridgeCollector"


# Consecutive failed cycles before we assume the dongle has lost the redirect
# and re-send it. At 5 s a cycle that is about half a minute of silence, which
# is long enough not to fire on a single dropped reply.
RELINK_AFTER = 6


def poll_forever(conn, log, idle: arbiter.Idle, *, panel: bool,
                 interval: float, relink=None, dongle: str | None = None) -> None:
    """The sampling loop. Blocks between cycles; never busy-waits."""
    qpiri = bridge.read(conn, "QPIRI", log).get("data")
    for command in config.SESSION_COMMANDS:
        if command != "QPIRI":
            bridge.read(conn, command, log)
            time.sleep(config.COMMAND_GAP)

    # The output-priority autopilot: decides SUB / SBU every cycle from the
    # last days' logs and the SOC. Advisory unless config.AUTO_PRIORITY.
    auto = autopilot.Autopilot(log, (qpiri or {}).get("output_source_prio"))

    last_clock_sync = time.time()
    on_battery_since: float | None = None
    failures = 0
    lent = False

    # The day book: the inverter's own per-day counters plus what we integrate
    # (battery, grid, time on battery). Persisted every ENERGY_INTERVAL.
    book = energy.DayBook(bridge.ENERGY_PATH)
    last_energy = 0.0
    history_pulled_for: dt.date | None = None

    def failed_cycle(reason: str) -> bool:
        """Count a failed cycle and, every RELINK_AFTER, re-send the redirect.

        The listener stays up throughout, so a dongle that reconnects on its
        own is picked up automatically. This is for the case where it does
        not -- and it did not, for five hours on 2026-09-20, because the
        original loop only counted a raised LinkError and a dead session
        fails every read *quietly* inside snapshot().
        """
        nonlocal failures, dongle
        failures += 1
        log({"event": "poll-failed", "error": reason, "run": failures})
        if relink is not None and failures % RELINK_AFTER == 0:
            log({"event": "relink", "after_failures": failures,
                 "dongle": dongle})
            try:
                answered = relink(dongle)
            except OSError as relink_exc:
                log({"event": "relink-failed", "error": str(relink_exc)})
            else:
                # The redirect also goes out by broadcast, so a dongle that
                # moved to a new DHCP address still answers -- from there.
                # Remember it: the next unicast, and the restore at
                # handover or exit, should go to where it is now.
                if answered and dongle not in answered:
                    log({"event": "dongle-moved", "from": dongle,
                         "to": answered[0]})
                    dongle = answered[0]
        return idle.wait(interval)

    while not idle.stopped:
        # A handover is a loan, not a shutdown: drop the session, give the
        # cloud endpoint back so the Android app has something to read, and
        # take the link again when the timer runs out.
        remaining = bridge.handover_remaining()
        if remaining > 0:
            if not lent:
                log({"event": "handover-start", "seconds": round(remaining)})
                session = getattr(conn, "session", None)
                if session is not None:
                    session.close()
                bridge.restore(dongle, announce=lambda *a: None)
                lent = True
            if not idle.wait(min(15.0, remaining)):
                break
            continue

        if lent:
            log({"event": "handover-end"})
            bridge.cancel_handover()
            if relink is not None:
                relink(dongle)
            if hasattr(conn, "wait_for_session"):
                conn.wait_for_session(120)
            lent = False
            failures = 0

        try:
            snap = bridge.snapshot(conn, log, qpiri=qpiri)
        except linkmod.LinkError as exc:
            if not failed_cycle(str(exc)):
                break
            continue
        if not snap["flow"]:
            # Every core read failed. bridge.read() swallows LinkError into
            # the record, so this is what a dead session looks like from here.
            problem = ((snap["records"].get("QPIGS") or {}).get("error")
                       or "no QPIGS data")
            if not failed_cycle(problem):
                break
            continue
        if failures:
            log({"event": "poll-recovered", "after_failures": failures})
        failures = 0

        analysis = snap["flow"]
        if analysis:
            if analysis.get("on_battery"):
                if on_battery_since is None:
                    on_battery_since = time.time()
                    log({"event": "on-battery", "load_w": analysis["load_w"],
                         "batt_v": analysis["batt_v"],
                         "discharge_a": analysis["batt_discharge_a"]})
                analysis["on_battery_seconds"] = round(time.time() - on_battery_since)
            elif on_battery_since is not None:
                log({"event": "off-battery",
                     "duration_s": round(time.time() - on_battery_since)})
                on_battery_since = None

            now = bridge.now_site()
            book.add_sample(now, time.time(), analysis)
            if time.time() - last_energy >= config.ENERGY_INTERVAL:
                # Today's counters every minute; the back-fill once per day,
                # because it is ~60 reads the first time and 2 after that.
                pull = (config.ENERGY_HISTORY_DAYS
                        if history_pulled_for != now.date() else 1)
                try:
                    bridge.refresh_energy(conn, book, now.date(), log, pull)
                    history_pulled_for = now.date()
                except linkmod.LinkError as exc:
                    log({"event": "energy-read-failed", "error": str(exc)})
                last_energy = time.time()
                book.save()
            today = book.summary(now.date())
            today["pv_verdict"] = energy.pv_verdict(
                today["yesterday_pv_wh"], today["pv_baseline_wh"],
                config.PV_DROP_WARN_FRACTION)

            try:
                policy_state = auto.step(conn, now, analysis)
            except Exception as exc:          # noqa: BLE001 -- never the loop
                log({"event": "priority-error", "error": repr(exc)})
                policy_state = None

            bridge.write_state(snap, today=today, policy=policy_state)
            if panel:
                print("\n" + flowmod.render(analysis, when=bridge.now_site(),
                                            warnings=snap["warnings"]),
                      flush=True)

        # Periodic clock resync, if it is wanted and permitted. Gated on the
        # clock's own switch -- it was on ALLOW_WRITES, which is False, so the
        # "every 24 h" the docs promised never ran.
        if ((config.ALLOW_CLOCK_WRITES or config.ALLOW_WRITES)
                and config.CLOCK_RESYNC_HOURS
                and time.time() - last_clock_sync >= config.CLOCK_RESYNC_HOURS * 3600):
            bridge.clock_sync(conn, log, announce=lambda *a: None)
            last_clock_sync = time.time()

        cadence = (config.BATTERY_POLL_INTERVAL
                   if analysis.get("on_battery") else interval)
        if not idle.wait(cadence):
            break


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--restore", action="store_true",
                        help="point the dongle back at its vendor cloud")
    parser.add_argument("--direct", metavar="IP",
                        help="connect out to the dongle; changes nothing on it")
    parser.add_argument("--dongle", metavar="IP",
                        help="dongle IP for the redirect path")
    parser.add_argument("--no-redirect", action="store_true",
                        help="listen only; do not touch dongle config")
    parser.add_argument("--panel", action="store_true",
                        help="render the power-flow panel instead of raw JSONL")
    parser.add_argument("--interval", type=float, default=config.POLL_INTERVAL,
                        help=f"on-grid poll seconds (default {config.POLL_INTERVAL})")
    parser.add_argument("--wait", type=float, default=300.0,
                        help="seconds to wait for the dongle to connect")
    parser.add_argument("--quiet", action="store_true",
                        help="log to logs/ only; do not echo (use when resident)")
    parser.add_argument("--autostart", choices=("on", "off"),
                        help="start this collector at login, and exit")
    args = parser.parse_args()

    if args.autostart:
        extra = ["--quiet"]
        if args.dongle:
            extra += ["--dongle", args.dongle]
        bridge.set_autostart(AUTOSTART_VALUE, args.autostart == "on",
                             "collector.py", extra)
        state = "enabled" if args.autostart == "on" else "disabled"
        print(f"Collector autostart {state}.")
        if args.autostart == "on":
            print("  " + bridge.launch_command("collector.py", extra))
        return 0

    if args.restore:
        bridge.restore(args.dongle)
        return 0

    # A moved folder would otherwise leave a stale autostart path that starts
    # nothing, with no symptom until the next reboot. The refresh carries the
    # same arguments --autostart on writes, or it would quietly drop --dongle.
    extra = ["--quiet"] + (["--dongle", args.dongle] if args.dongle else [])
    bridge.refresh_autostart(AUTOSTART_VALUE, "collector.py", extra)

    log = (bridge.quiet_logger() if (args.panel or args.quiet)
           else bridge.make_logger())
    idle = arbiter.Idle()

    claim = arbiter.LinkClaim()
    if not claim.try_acquire(5000):
        print("Another phantom-bridge process holds the dongle link.")
        print("Stop it (or exit the tray) and try again -- the dongle keeps "
              "only one session, so they cannot share.")
        return 1

    conn = None
    try:
        conn = bridge.open_link(direct=args.direct, dongle=args.dongle,
                                redirect=not args.no_redirect,
                                wait=args.wait, log=log)

        if config.CLOCK_SYNC_ON_CONNECT:
            bridge.clock_sync(conn, log)

        print(f"\nPolling every {args.interval:g}s on grid, "
              f"{config.BATTERY_POLL_INTERVAL:g}s on battery. Ctrl-C to stop.\n")

        relink = None
        if not args.direct and not args.no_redirect:
            relink = lambda ip: bridge.send_redirect(ip,
                                                     announce=lambda *a: None)

        # Once connected, the session's peer is where the dongle actually is;
        # --dongle and config.DONGLE_IP are only the first guess.
        session = getattr(conn, "session", None)
        dongle_ip = getattr(session, "peer", None) or args.dongle
        if args.dongle and dongle_ip != args.dongle:
            log({"event": "dongle-moved", "from": args.dongle, "to": dongle_ip})

        worker = threading.Thread(
            target=poll_forever, args=(conn, log, idle),
            kwargs={"panel": args.panel, "interval": args.interval,
                    "relink": relink, "dongle": dongle_ip},
            daemon=True)
        worker.start()

        # Park here. No sleep loop -- the main thread blocks in the kernel
        # until Ctrl-C sets the event.
        idle.block_until_stopped()
    except linkmod.LinkError as exc:
        print(f"\n{exc}")
        return 1
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        idle.stop()
        if conn is not None:
            conn.close()
        claim.close()
        if not args.no_redirect and not args.direct:
            print("The dongle is still pointed at us. To give it back to "
                  "WatchPower:\n  python collector.py --restore")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
