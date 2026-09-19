# phantom-bridge — context for Claude Code

Local monitoring/control of a **Long Life Phantom II (VM4 6000)** hybrid
inverter over WiFi, bypassing the WatchPower vendor cloud.

Read `README.md` for the how, `docs/PI30-COMMANDS.md` for the command set,
`docs/FINDINGS.md` for the site baseline and what the hardware settled.

**This is no longer a spike.** First contact was 2026-09-19 and it worked:
the link, the framing, the telemetry and the clock write are all confirmed
against the unit. What follows is measured unless it says otherwise.

## Hardware facts (confirmed against the unit, 2026-09-19)

- Inverter: Long Life Phantom II, **one 6 kVA / 6000 W chassis**, 48 V bus.
  The owner's "VM4 6000 twin" is a **single** machine: `QPGS0..3` all NAK and
  `QPIRI` reports `parallel_max_units = 1`, `output_mode = 0`. Settled.
- Serial `<inverter-serial>`, firmware `VERFW:00060.10`, protocol `PI30`.
- Wi-Fi module PN `W00344XXXXXXXX`, firmware `3.6.6.6`, at **`<dongle-ip>`**.
  Eybond/Shinemonitor-family collector (SoftAP SSID = PN, pw `12345678`).
- Site is in Asia/Karachi (UTC+5).
- Charger source priority is **3 (Solar only)** with max utility charge
  current **2 A** — the pack is configured to charge from PV essentially
  alone. Worth knowing before touching anything charge-related.

## The approach

Send `set>server=<host>:8899;` to the dongle on **UDP 58899**. It then opens
its TCP session to us instead of the vendor cloud. Over that link it is a raw
RS-485 passthrough: 8-byte Eybond header, function code `4`, then an
unmodified PI30 command.

```
[tid:2][devcode:2][wire_len:2][devaddr:1][fc:1][ PI30 payload ]
```

The dongle exposes **no listening TCP port of its own** (8899, 502, 80, 23,
18899 all closed), so the redirect is the only way in — and WatchPower does go
quiet while we hold the link. `python collector.py --restore` gives it back.

## What is proven vs. assumed

**Proven against the unit:** the PI30 wrapping in `src/frames.py`; the Eybond
header including `devcode 0x0993`, `fc=4` and the `wire_len = payload + 2`
rule (dialect `tail`, auto-confirmed from the first CRC-valid reply);
`set>server=` redirect and `set>server=default;` restore; the whole read
catalogue below; and `DAT` clock setting, verified by `QT` readback.

**Still assumed:** the three extra `QPIGS` tail fields are mapped as the
grid-feed extension (`solar_feed_to_grid`, `country_code`, `solar_feed_w`) —
plausible names, but none has been made to move. The FC=1 heartbeat payload
shape is **untested and moot**: across six sessions this dongle sent no
heartbeats at all.

If hardware contradicts this file, hardware wins — update the file.

## Current state

Everything the original gate asked for is done.

- [x] Dongle answers on UDP 58899 — `rsp>server=1;` from `<dongle-ip>`
- [x] Dongle connects to our TCP 8899 after redirect (~2 s)
- [x] `QMOD` returns a mode character
- [x] `QPIGS` parses into sane values — **24 fields, not 21**
- [x] `QPIRI` + `QPGS*` settled the chassis question — **one chassis**
- [x] `DAT` accepted **and confirmed to move the clock** (the repo expected
      this to fail; it does not on this firmware)

Not done, deliberately: no power-behaviour write has ever been sent.

## Goals, in priority order

1. **Internet clock sync.** Solved and proven. `ctl.py time --sync` reads NTP,
   sends `DAT<YYMMDDHHMMSS>`, then reads `QT` back and compares — an `ACK` is
   never taken as evidence. Runs on connect and every 24 h.
2. **Load flow, especially on battery.** `src/flow.py` derives the grid leg
   from the energy balance (PI30 has no grid-power field) and renders a panel.
   On-battery is `QMOD == B` **and** discharge current > 0 — both, not either.
   Cadence tightens to 1 s automatically on battery, 5 s otherwise.
3. **All the controls.** The full PI30 setter catalogue is in `src/pi30.py`
   and exposed via `ctl.py set`, behind `ALLOW_WRITES`.
4. **Battery readout in the tray.** `tray.py`, stdlib ctypes Win32.
5. **A watch screen worth replacing the app with.** `web.py` serves
   `web/index.html` on the LAN: every source and every load at once on one
   shared scale, watts leading, volts and hertz demoted behind a fold. It is
   web rather than a native window for one reason -- the vendor app lives on
   a phone, and a LAN page is the only thing that reaches it. It is LAN-only
   and needs this PC up, which the cloud app is not; that is the trade.

## Layout

| File | What |
|---|---|
| `config.py` | Every site-specific value. `config.local.py` overrides it, gitignored. |
| `probe.py` | Read-only recon: find the dongle, report which transport is available. |
| `collector.py` | The resident poller. Redirect, poll, log JSONL, publish `logs/state.json`. |
| `ctl.py` | One-shot CLI: reads, writes, clock, discovery sweep, restore. |
| `tray.py` | Windows notification-area battery readout. |
| `web.py` + `web/` | The watch screen: a LAN dashboard reading `state.json`. |
| `src/frames.py` | CRC, escaping, Eybond framing, dialect detection. Doctested. |
| `src/pi30.py` | Command catalogue + response decoders. Doctested. |
| `src/link.py` | Transport: server/direct/dry-run links, sessions, correlation. |
| `src/bridge.py` | Shared connect/log/poll/clock-sync used by both entry points. |
| `src/flow.py` | Power-flow derivation and the ASCII panel. |
| `src/clock.py` | SNTP client and clock encoding. Doctested. |
| `src/arbiter.py` | Named-mutex link arbitration and the no-spin idle wait. |
| `src/netutil.py` | LAN address / broadcast / firewall helpers. |

## Conventions

- Python 3.10+, **standard library only**. Still true, including the Win32
  tray — it is ctypes, not pywin32. Keep it that way.
- On this machine `python3` does not exist. Use `python`.
- `config.py` holds every site-specific value. Nothing hardcoded elsewhere.
- `ALLOW_WRITES` stays `False`. It gates power behaviour. The clock is gated
  separately by `ALLOW_CLOCK_WRITES`, which is on, because an RTC cannot make
  units fight or mistreat a battery.
- `docs/FINDINGS.md` is append-only and dated. Log what the hardware says,
  including the failures.
- Console output is **ASCII only** — the Windows console code page will not
  render box-drawing characters.

## Cost and contention

Borrowed wholesale from RouterOps, for the same reasons:

- **Nothing spins.** The tray's UI thread parks in `GetMessageW`; every poll
  loop waits on an event with a timeout. There is no `while True: sleep(1)`
  anywhere, and there should not be.
- **One talker.** The dongle keeps exactly one server session and there is one
  RS-485 master behind it. `Local\PhantomBridge.Link` arbitrates: the
  foreground task wins, the resident readout yields, drops its session and
  shows cached state rather than a stale number.
- **The tray costs nothing by default.** In `--follow` mode it opens no link
  at all — it reads `logs/state.json` that the collector publishes. Zero extra
  RS-485 traffic, zero extra sessions.
- Poll cadence is a deliberate choice, not a default: 5 s on grid, 1 s on
  battery, and `PARALLEL_COMMANDS` is empty because `QPGS*` only NAKs here.

## Sharing with WatchPower

WatchPower here is the **Android** app; it is not installed on this PC. It
reads the **vendor cloud**, not the inverter. Two consequences, both settled:

- **They cannot run at once.** One server session, no listening port on the
  dongle. It points at the cloud or at us.
- **The swap cannot be automatic.** There is no local event when the app
  opens, so nothing to trigger on. (If that is ever wanted, the only lead
  worth trying is sniffing UDP 58899 for the app's own local discovery
  broadcasts — speculative, not attempted.)

The substitute is a timed loan: `ctl.py handover --minutes 15`, or the tray
menu. It writes `logs/handover.json`; the collector honours it on its next
cycle by dropping the session and restoring the cloud endpoint, then re-takes
the link when it expires. The app needs a minute or two of cloud uploads
before it shows current data, so short loans are wasted.

## Running live

Both entry points are resident services, started at login from
`HKCU\...\CurrentVersion\Run` (`PhantomBridgeCollector`,
`PhantomBridgeTray`), written by `bridge.set_autostart()` and refreshed on
every launch so a moved folder self-heals. Measured idle cost: ~23 MB and
~0.03 s CPU per minute each.

## Safety

- Redirecting the dongle silences the WatchPower app, and on this dongle there
  is no alternative route. Reversible via `python collector.py --restore`.
  Always leave that path working.
- On a parallel stack, never write output/charger priority to one unit alone.
  Mismatched units fight each other. **This site is a single chassis**, so the
  rule does not bite here — but `ctl.py` still enforces it, and that
  enforcement should stay for the next machine.
- The user is a developer (Python, Next.js, PHP). Skip the basics.
