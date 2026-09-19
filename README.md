# phantom-bridge

Local monitoring and control for a **Long Life Phantom II (VM4 6000)** hybrid
inverter, over WiFi, without the vendor cloud.

The dongle stays in place. We just stop letting it phone home.

**Status: working against the real unit since 2026-09-19.** The link, the
framing, the telemetry and the clock write are all confirmed. See
`docs/FINDINGS.md` for the measurements.

---

## Why

WatchPower works, but it is a menu, not a bus:

| Gap | What we do instead |
|---|---|
| Inverter clock drifts, silently | NTP -> `DAT`, verified by `QT` readback |
| Coarse load monitoring | `QPIGS` at 1–5 s, decoded into named fields |
| Grid/battery state buried in a UI | `QMOD` + discharge current, machine-readable |
| Vendor-chosen settings only | The full PI30 setter catalogue |
| Nothing at a glance | A battery percentage in the notification area |

## How it works

The Wi-Fi module (PN `W00344XXXXXXXX`, fw `3.6.6.6`) is an
Eybond/Shinemonitor-family collector. It listens on **UDP 58899** for
`set>...;` config strings. Send it `set>server=<our-ip>:8899;` and it opens
its TCP session to us instead of the vendor cloud.

Over that TCP link it is a **raw RS-485 passthrough**: an 8-byte Eybond header
with function code `4`, then an unmodified PI30 command.

```
[tid:2][devcode:2][wire_len:2][devaddr:1][fc:1][ PI30 payload ]
                                                 QPIGS + CRC16-XMODEM + CR
```

All of that is now confirmed, including `wire_len = payload + 2` — the rule
that was the weakest guess in the original spike. The dongle exposes **no
listening TCP port of its own**, so the redirect is the only way in, and
WatchPower does go quiet while we hold the link.

## Layout

```
config.py        site settings - edit this first
probe.py         recon: find the dongle, report which transport is available
collector.py     the resident poller: redirect, poll, log, publish state
ctl.py           one-shot CLI: reads, writes, clock, discovery, restore
tray.py          Windows notification-area battery readout
web.py           the watch screen: local dashboard, phone-reachable
web/index.html   that dashboard, one self-contained file
src/frames.py    CRC, escaping, Eybond framing, dialect detection
src/pi30.py      command catalogue + response decoders
src/link.py      transport: server / direct / dry-run links
src/bridge.py    shared connect, log, poll and clock-sync
src/flow.py      power-flow derivation + the ASCII panel
src/clock.py     SNTP client and clock encoding
src/arbiter.py   link arbitration and the no-spin idle wait
src/netutil.py   LAN address, broadcast and firewall helpers
logs/            one JSONL file per day, plus state.json for the tray
```

## Running it

Must run on a machine **on the same WiFi as the inverter**. Python 3.10+,
standard library only. On this machine the interpreter is `python` —
`python3` does not exist.

```bash
# 1. recon - read-only, changes nothing
python probe.py

# 2. does a link come up, and what is this machine?
python ctl.py probe-link

# 3. the one that answers every open question at once
python ctl.py discover

# 4. live power flow
python ctl.py status
python ctl.py watch

# 5. the clock
python ctl.py time              # show drift against internet time
python ctl.py time --sync       # set it, then prove it moved

# 6. resident logging
python collector.py             # JSONL to logs/, state.json for the tray
python collector.py --panel     # the flow panel instead of raw JSON

# 7. the tray readout (no console window)
pythonw tray.py

# hand the dongle back to the vendor cloud
python collector.py --restore
```

Windows Firewall will silently drop the dongle's inbound connection on a
Public-profile network, which looks exactly like "the redirect did not work".
Once, elevated:

```
netsh advfirewall firewall add rule name="phantom-bridge 8899" dir=in action=allow protocol=TCP localport=8899
```

## The controls

`python ctl.py controls` lists all 25 setters with their accepted values.
Writing one:

```bash
python ctl.py set output-priority sbu --yes
python ctl.py set float-voltage 54.0 --yes
python ctl.py set max-charge-current 60 --unit 0 --yes
```

Every write is refused unless `ALLOW_WRITES = True` in `config.py`, and the
refusal happens **before** any link is opened — being told "no" should never
cost a redirect that silences WatchPower. Parallel-sensitive settings need an
explicit `--unit` or `--all-units`, and `factory-reset` needs `--i-mean-it`.

The clock is gated separately by `ALLOW_CLOCK_WRITES`, which is on: an RTC
cannot make units fight each other or mistreat a battery, so it does not
belong behind the same gate as charge voltages.

## The tray readout

`pythonw tray.py` puts the state of charge in the notification area: the exact
percentage drawn into the icon, with a proportional bar under it, coloured by
what is actually trustworthy — work mode and discharge current, not the
percentage.

- green: charging | blue: on grid, idle | amber/orange/red: on battery
- grey: no data, or another task holds the link
- Right-click for refresh, logs, clock sync, restore and start-at-login.

By default it opens **no link at all**: it reads `logs/state.json` that the
collector publishes each cycle. Zero extra RS-485 traffic, zero extra dongle
sessions.

## The watch screen

```bash
python web.py            # http://localhost:8080, and http://<this-pc>:8080 from a phone
```

The vendor app shows you one thing at a time and leads with volts and hertz.
This shows **every source and every load at once**, on one shared scale, so a
bar on the left means the same number of watts as a bar on the right:

```
   SOURCES                LOAD NOW               LOADS
   Solar       0 W          410 W          Household    410 W
   Grid      410 W         ~~~~~~~         Battery in     0 W
   Battery     0 W
```

Watts lead everywhere. Volts, hertz, VA and power factor are real but
secondary, so they live behind one collapsed row instead of competing for
attention. Below the board: state of charge, battery voltage and current,
and -- when it is on battery -- how long it has left.

It reads `logs/state.json` and opens no link to the inverter, so it adds zero
RS-485 traffic and never competes with the collector or the tray. Serve it
always with `python web.py --autostart on`.

**Where it is worse than the vendor app:** it works on your WiFi only, while
this PC is running. The app reads the cloud, so it works from anywhere. This
is a better screen, not a wider one.

## Sharing with the WatchPower app

**They cannot both have it at once.** The dongle keeps exactly one server
session and exposes no listening port of its own, so it points at either the
vendor cloud or at us. While phantom-bridge holds the link, the Android app
has nothing to read.

Nor can the swap be automatic: the Android app talks to the **cloud**, not to
the inverter, so there is no local event to detect when you open it.

What there is instead is a timed loan:

```bash
python ctl.py handover --minutes 15   # give the app the dongle for 15 minutes
python ctl.py handover --cancel       # take it back early
```

or the tray's **"Lend to WatchPower app (15 min)"** menu item. The running
collector restores the cloud endpoint on its next cycle, stops polling, and
re-takes the link when the timer runs out. The tray icon goes grey and reads
"Lent to WatchPower — N min left" for the duration.

Give the app a couple of minutes before expecting current numbers: it is
reading the cloud's copy, and the cloud has to receive fresh uploads first.
That is why the default loan is 15 minutes and not 30 seconds.

## Running it as a service

```bash
python collector.py --autostart on --dongle <dongle-ip>
python collector.py --quiet --dongle <dongle-ip>   # resident, logs only
pythonw tray.py                                      # tray, no console
```

Autostart writes to `HKCU\...\CurrentVersion\Run` and is rewritten on every
launch, so moving the folder self-heals rather than silently starting nothing
after the next reboot. Turn it off with `--autostart off`, or untick
"Start at login" in the tray menu.

Measured idle cost with both resident: ~23 MB each and roughly 0.03 s of CPU
per minute.

## Cost and contention

Taken from RouterOps, for the same reasons:

- **Nothing spins.** The tray's UI thread parks in `GetMessageW`; poll loops
  wait on an event with a timeout. There is no `while True: sleep(1)`.
- **One talker.** The dongle keeps one session and there is one RS-485 master
  behind it. A named mutex arbitrates: the foreground task wins, the resident
  readout yields and shows cached state rather than a stale number.
- **Cadence is a choice.** 5 s on grid, 1 s once actually on battery, which is
  the only time a second of resolution earns its traffic.

## Cautions

- Redirecting silences the WatchPower app, and on this dongle there is no
  alternative route. Reversible: `python collector.py --restore`.
- `ALLOW_WRITES` is `False`. Reads are boring now, but nothing here has needed
  a power-behaviour write, so the gate stays shut until something does.
- On a parallel stack, never set output priority on one unit alone —
  mismatched units fight each other. **This site is a single chassis**, so it
  does not bite here, but `ctl.py` still enforces it.
