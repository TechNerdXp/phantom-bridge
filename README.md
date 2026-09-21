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
| Nothing at a glance | A battery percentage in the notification area, and a watch screen |
| History is a graph in an app | The inverter's own daily counters, 30 days back, and a usage report from the logs |

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
watch.py         the watch screen: a native GDI/GDI+ window, opened from the tray
insights.py      usage report from the logs: peaks, sun hours, battery episodes
src/frames.py    CRC, escaping, Eybond framing, dialect detection
src/pi30.py      command catalogue + response decoders
src/link.py      transport: server / direct / dry-run links
src/bridge.py    shared connect, log, poll, energy counters and clock-sync
src/flow.py      power-flow derivation + the ASCII panel
src/energy.py    the day book: inverter counters plus integrated battery/grid
src/clock.py     SNTP client and clock encoding
src/arbiter.py   link arbitration and the no-spin idle wait
src/netutil.py   LAN address, broadcast and firewall helpers
logs/            one JSONL file per day, state.json for the tray, energy.json per day
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

# 8. what the last week looked like -- reads the logs, opens no link
python insights.py
python insights.py --date 2026-09-19

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

## Automatic output priority

The one setting the owner changes twice a day -- SBU or SUB -- is decided by
the collector from the logs instead of by the inverter's own timer (menu 99).
The rule, in `src/policy.py`:

- **Day**, from the turnaround to dusk: **SBU**. Solar and the pack carry the
  house.
- **Dusk** -- the end of the last hour whose PV was still a quarter of the
  best hour, yesterday: **SUB**. The grid carries the house and the pack is
  kept as the reserve an outage will need. If the grid drops, the inverter
  uses the pack regardless.
- **Night release**: every cycle it works out how many watt-hours the house
  will draw from now until the turnaround (the hourly load over the last
  week, over the measured battery-to-load efficiency) and compares it with
  what the pack holds above the floor. The moment they match it switches to
  **SBU**, so the pack lands on the floor just as the sun starts lifting it.
  Eleven, two, three -- the load decides, and an outage that spent some of
  the reserve pushes the release later on its own.
- **Turnaround**: yesterday's lowest-SOC time, when it fell between 03:00 and
  noon -- the minute the sun started pushing the pack up.
- **Floor**: at or under 30 %, at any hour, **SUB**, held until the sun has
  lifted the pack past 35 % inside the day window. The 5-point gap is what
  stops it flapping at dawn.

`python ctl.py auto` prints the profile it read (turnaround, dusk, pack Wh
per SOC point, efficiency, the night's hourly load), the verdict for this
moment, and the release time for every SOC from here -- with no link
opened. The collector logs `priority-plan` once a day, `priority-decision`
on every change of mind, and the watch screen's status line and the tray
menu carry the same verdict.

It ships **advisory**: `AUTO_PRIORITY = False` computes and shows everything
and writes nothing. Turn the inverter's own timer off first (menu 99), then
set `AUTO_PRIORITY = True` in `config.local.py`; from then on the collector
sends `POP01` / `POP02`, waits, reads `QPIRI` back and logs `priority-set`
with the verdict -- an ACK is never taken as proof, exactly as for the
clock. It re-reads `QPIRI` every five minutes regardless, and a change it
did not make is logged as `priority-external-change` (that is what the
timer, or a hand on the panel, looks like). No two writes within ten
minutes, except the floor rule, which is protective. Every number above is
in the "Automatic output priority" block of `config.py`.

## The tray readout

`pythonw tray.py` puts the state of charge in the notification area: the exact
percentage drawn into the icon, with a proportional bar under it, coloured by
what is actually trustworthy — work mode and discharge current, not the
percentage.

- green: charging | blue: on grid, idle | amber/orange/red: drawing on the pack
- the headline says who is carrying the house: on solar, on grid, solar +
  battery, on battery, grid out. Not the raw work mode -- `QMOD = B` is
  inverter mode, which is where the unit sits all day on solar.
- grey: no data, or another task holds the link
- a leg at its limit (grid line, home lines or battery -- the same rule as
  the watch screen's arrows): a red mark in the top-right corner, alternating
  with the plain icon every half second. The digits and the bar stay the
  battery's, in the battery's colour. The tooltip names the leg.
- Right-click for refresh, logs, clock sync, restore and start-at-login.

By default it opens **no link at all**: it reads `logs/state.json` that the
collector publishes each cycle. Zero extra RS-485 traffic, zero extra dongle
sessions.

## The watch screen

Left-click the tray icon (or **Open watch screen** in its menu). A native
window, painted with GDI and GDI+ in the tray's own process -- no browser, no
console, no second interpreter, no port.

The vendor app shows one thing at a time and leads with volts and hertz. This
shows **the four pillars of the energy balance at once** -- solar above, grid
left, home right, battery below -- around the inverter, with an arrow on each
leg. The arrow points the way the current flows and its thickness is the
watts, on **one shared scale**, so a fat arrow means the same thing on every
leg. Watts lead at every pillar, in the big face; the volts, amps, hertz, VA
and power factor sit under them in a small monospace face for whoever wants
them. The battery pillar is a tank: a liquid level inside a circle with the
percentage on it, the empty part drawn as the tank body, so 40% reads as
60% empty at a glance.

```
  Phantom II                                          SOLAR + BATTERY
                        [ solar ]
                         1,240 W
                       312 V  4.0 A
                            |
    [ grid ]   ---->   [ PHANTOM II ]   ---->   [ home ]
     0 W                 INVERTER                1,650 W
  235 V 50.0 Hz           mode B              220 V 50.0 Hz  7.5 A
  present, on standby       ^                 1,700 VA  pf 0.97  27% of 6 kVA
                            |
                        ( 66 % )
                         -476 W
                   52.9 V  9 A  discharging

  Drawing on the pack by priority (SBU)                        history >
```

The header is the verdict -- who is carrying the house -- and not the raw
work mode. `QMOD = B` is "battery mode" in the vendor's words, but on this
family it means the inverter stage is making the AC from the DC bus, which
the sun feeds too; the unit sits in B all day on solar with the pack
charging. The screen used to print that as ON BATTERY. Now the box says
INVERTER or GRID BYPASS and the header says ON SOLAR, SOLAR + BATTERY,
ON BATTERY, GRID OUT or ON GRID from the currents.

Under the board there is one line of status and, at the right, "history >",
which opens the history window. The day's figures and the cable bar that
used to sit here live there now; the arrows carry the limits.

Every arrow also wears its own limit. The grid leg is judged against the
7/29 on the input (`GRID_LINE_RATING_A`; its amps are derived, watts over
grid volts), the home leg against one line's rating with both lines summed
(the split is unknown, so red means one line *could* be at its limit), and
the battery leg against the pack (`BATTERY_CONTINUOUS_A` amber,
`BATTERY_MAX_A` red, charging and discharging alike). Amber near the limit,
red at it, and at it the arrow, its figure and the status line blink on a
half-second timer that runs only while something is at its limit.

It repaints on `WM_APP_SAMPLE` -- once per collector sample, and only while
the window is open. **A closed window costs nothing**: no timer, no poll, no
thread. That is why it is a window and not a web page; a browser tab costs
100-300 MB to render 800 bytes of JSON. `python watch.py --png out.png`
renders one frame to a file, which is how the layout is checked.

No notifications. A balloon on grid-lost/grid-back was tried and removed on
request: the inverter flips between line and battery mode every few seconds
around dawn while the sun is not quite enough, and each flip was a balloon.
`TRAY_NOTIFICATIONS` in config turns it back on for anyone who wants it.


## The power history

**Power history** in the tray menu, or click the day strip at the bottom of
the watch screen. The same native window plumbing, four tabs:

- **Day**, midnight to midnight: solar as a filled area, the house as a
  line, the derived grid leg as a line, the state of charge as a thin line
  on its own 0-100 % axis on the right. Under it the battery's own strip,
  charging up from the centre in green and discharging down in amber. Every
  on-battery episode is shaded amber across both, every grid outage red, so
  *when the house runs on the pack, and why* is visible without reading a
  number. Curves break where the log has a hole rather than bridging it.
  Then the day's facts, its on-battery episodes (when, how long, watt-hours,
  average draw, SOC start to end, the pack capacity that implies, amber for
  "by priority" and red for "outage") and its hour-by-hour profile.
- **Week** and **Month**: a pair of bars per day, solar and house kWh, with
  the battery's share of the load as an amber inset and the trailing 7-day
  typical solar as a line -- the panel-cleaning trend. Under it the typical
  hour of the range: average house and solar by hour, and how much of each
  hour the pack was carrying. Then the review: totals, medians, best and
  worst days, when usage peaks, when the sun carries the house, when the
  pack is typically fullest and lowest, how often it ran on battery and
  why, and the pack capacity the episodes imply.
- **Year**: the same review with one bar per month.

The history is **complete since inception**, not a window of days. Each
finished day's analysis is computed once from its log and cached as
`logs/days/<date>.json` (keyed on the log's size, so a changed log is
re-read), which makes a year in review a read of a few hundred small files
rather than a parse of the JSONL. Days before logging began still appear
with the inverter's own daily counters. Today is live: its log is read
incrementally, only the lines appended since the last look, once a minute
while the window is open, and dropped when it closes.

Left / Right step by the tab's unit, Home returns to today, D W M Y switch
tabs. Click a day bar to open that day, a month bar to open that month.
`python history.py --date 2026-09-19 --tab week` opens standalone;
`--png out.png` renders a frame. Reads `logs/`, never the link.

## Usage insights

```bash
python insights.py                # the last 7 days, and the shape across them
python insights.py --days 30
python insights.py --date 2026-09-19
```

The same analysis, drawn, is the history window's week / month / year
review (above); this is the text form for a terminal, a file (`--out`) or
anything downstream (`--json`). It reads the logs only and never touches
the link.

Per day: energy (the inverter's counters for solar and load, integrated
battery in/out and derived grid), when the load peaked, when the sun carried
the house, when the pack was fullest and lowest, every on-battery episode
with the watt-hours drawn and the SOC it cost, every grid outage, and an
hour-by-hour table. Across the run: the hours usage peaks, the hours the
house is usually on battery, and the pack capacity the episodes imply.

Two things it says out loud rather than hides. The SOC is the inverter's
voltage estimate, so the implied capacity is indicative; the watt-hours are
measured. And an on-battery episode with the grid present is the **priority
setting** at work, not an outage -- on this site the house runs from the pack
overnight with the grid up, which is where much of the "backup" goes before
any outage starts.

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

The two resident services ship as one exe, built the RouterOps way:

```bash
pyinstaller PhantomBridge.spec --clean       # -> dist\PhantomBridge\PhantomBridge.exe
dist\PhantomBridge\PhantomBridge.exe collector --autostart on --dongle <dongle-ip>
dist\PhantomBridge\PhantomBridge.exe collector --quiet --dongle <dongle-ip>
dist\PhantomBridge\PhantomBridge.exe tray    # tray + watch screen; ticks its own autostart
```

`dist\PhantomBridge\` is the installation: rebuild after editing any `.py`.
The exe has no console, so it carries only the tray and the collector;
`ctl.py`, `insights.py`, `probe.py` and `collector.py --panel` are run from
the checkout with `python`, and they read the same `logs/` and
`config.local.py` the exe does. From source the same two services are
`python collector.py --quiet --dongle <dongle-ip>` and `pythonw tray.py`.
The exe costs what `pythonw` costs; what it adds is its own icon and name
everywhere Windows shows a process, and independence from the installed
Python. An unhandled exception in the windowed exe lands in `logs/crash.log`.

Autostart writes to `HKCU\...\CurrentVersion\Run` and is rewritten on every
launch, so moving the folder self-heals rather than silently starting nothing
after the next reboot. Turn it off with `--autostart off`, or untick
"Start at login" in the tray menu. A source run leaves an entry that points
at an existing exe alone.

Measured idle cost with both resident: ~23 MB each and roughly 0.03 s of CPU
per minute. The watch screen adds no process -- it is a window in the tray.

## Cost and contention

Taken from RouterOps, for the same reasons:

- **Nothing spins.** The tray's UI thread parks in `GetMessageW`; poll loops
  wait on an event with a timeout. There is no `while True: sleep(1)`.
- **One talker.** The dongle keeps one session and there is one RS-485 master
  behind it. A named mutex arbitrates: the foreground task wins, the resident
  readout yields and shows cached state rather than a stale number.
- **Cadence is a choice.** 5 s on grid, 1 s once actually on battery, which is
  the only time a second of resolution earns its traffic.

## Is it internet dependent?

No. The dongle, the collector, the tray, the watch screen and the insights
report are all on the LAN and keep working with the internet down. The one
thing that reaches out is the clock sync: an SNTP query with a three-second
timeout per server, tried on connect and every 24 h. With no internet it
logs "no NTP server answered" and moves on; the logs are stamped from the PC
clock in that case. The router and WiFi do have to be up -- the dongle is
reached over them.

## Cautions

- Redirecting silences the WatchPower app, and on this dongle there is no
  alternative route. Reversible: `python collector.py --restore`.
- `ALLOW_WRITES` is `False`. Reads are boring now, but nothing here has needed
  a power-behaviour write, so the gate stays shut until something does.
- On a parallel stack, never set output priority on one unit alone —
  mismatched units fight each other. **This site is a single chassis**, so it
  does not bite here, but `ctl.py` still enforces it.
