# phantom-bridge — context for Claude Code

Local monitoring/control of a **Long Life Phantom II (VM4 6000)** hybrid
inverter over WiFi, bypassing the WatchPower vendor cloud.

Read `README.md` for the how, `docs/PI30-COMMANDS.md` for the command set,
`docs/FINDINGS.md` for the site baseline and what the hardware settled.

## Where it lives

`D:\PowerTools\phantom-bridge` -- moved here from the Desktop on 2026-09-20,
because it is a machine tool. Like RouterOps it is **its own git repo**
(`github.com/TechNerdXp/phantom-bridge`) nested inside the PowerTools tree
and ignored by the parent repo. Commit here, not at `D:\PowerTools`.

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
  That address is a **DHCP lease, not a fact**: it moved once (2026-09-20,
  when the router it hangs off was restarted) and the collector lost the
  link for 28 minutes because it re-sent the redirect to the old address
  only. Every `set>` now goes unicast *and* broadcast, and the collector
  follows the dongle (`dongle-moved` in the log). A DHCP reservation on
  that router is the durable fix and has not been made.
- Site is in Asia/Karachi (UTC+5).
- Charger source priority is **3 (Solar only)** with max utility charge
  current **2 A** — the pack is configured to charge from PV essentially
  alone. Worth knowing before touching anything charge-related.
- The pack was **sold as 5 kWh and delivers about 2 kWh** (the owner,
  2026-09-22; Oracle #29 is the open investigation). The on-battery
  episodes read the inverter's voltage-derived SOC and that scale implies
  the sold figure (4.8 kWh per 100 % over the first five), so it is not
  evidence either way. Until the pack is measured, every derived pack
  figure is capped at `BATTERY_PACK_WH_CAP` (2000 Wh): the data may say
  less, never more. `BATTERY_SOLD_WH` is only for the reports' comparison
  line. A bigger measured pack raises the cap; do not raise it on the
  SOC scale alone.
- **The inverter's SOC is an estimate with a clock in it** (2026-09-22,
  `_labs/`): it slides ~2 points/hour at rest at every level -- even
  while the sensor shows 1 A going in, and while the pack sits at 57.6 V
  float -- and it clamps at 100. Charge side 44 Wh/point in, discharge
  side ~54 out, which no counter does. The pack itself takes 2.7-3.1 kWh
  every morning from the 30-40 % state to a real full, so it holds at
  least ~2.5 kWh above that state; below it is unmeasured. Never treat a
  SOC slide as a drain, and never size the pack from the SOC scale.
- Output source priority is **2, SBU** (Solar-Battery-Utility; `QPIRI`
  field 17 -- an earlier note said 1, wrong), so the house runs from the
  battery overnight **with the grid present** (seen 2026-09-20: mode B, 9 A
  discharge, 242 V on the input). A real outage therefore starts from a
  part-drained pack. Not static either: the inverter's own timer (menu 99)
  flips it to SUB in the evening and back to SBU at 02:00 (QPIRI reads
  2026-09-19..22; the 02:00-07:21 "by priority" episode on the 21st). The
  autopilot below replaces that timer once it is cleared.
- **`QMOD = B` is inverter mode, not "on battery".** The unit sits in B all
  day on solar with the pack charging (266 samples on 2026-09-20). B means the
  inverter stage makes the AC from the DC bus; L means grid bypass. Who is
  actually carrying the house is `flow.source` (solar / solar+battery /
  battery / grid), and that is what every screen must lead with. Never map
  the letter to a verdict again.
- The per-day energy counters `QED`/`QLD` (and month/year forms) answer and
  start on 2026-09-04. They are the source for "today" and the panel trend.

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

**Still assumed:** the first two extra `QPIGS` tail fields are mapped as the
grid-feed extension (`solar_feed_to_grid`, `country_code`) and have never
moved. The third is **not** solar feed (it reads 70-80 in the dark) and is
carried as `unknown_24`. The FC=1 heartbeat payload shape is **untested and
moot**: across six sessions this dongle sent no heartbeats at all.

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
- [x] Daily energy counters read every minute, 30 days back-filled per
      session, into `logs/energy.json`
- [x] The link recovers on its own: a cycle with no data counts as a failure
      and the redirect is re-sent every six, by unicast and broadcast (it
      did not, for five hours and then for 28 minutes, on 2026-09-20 -- see
      FINDINGS)

- [x] The SUB/SBU autopilot, advisory: decides every cycle, logs and shows
      it, writes nothing until `AUTO_PRIORITY` is on (2026-09-22)

Not done, deliberately: no power-behaviour write has ever been sent. The
first one will be a `POP` from the autopilot, after the inverter's timer
(menu 99) is cleared and `AUTO_PRIORITY = True` is set in `config.local.py`.

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
4. **Battery readout in the tray.** `tray.py`, stdlib ctypes Win32. The
   icon's colour is the owner's ladder of concerns (not full, heavy draw,
   house on the pack alone: green/blue/orange/red, with low and critical
   on top), consolidated in one place: the tray block of `config.py`, the
   comment over the palette in `tray.py` and `Reading.colour` under it.
   The thresholds are sized to this ~2 kWh pack; a bigger bank weighs
   them differently (noted in that config block). Green, red and grey match RouterOps
   because the two icons share a tray.
5. **A watch screen worth replacing the app with.** `watch.py`: a native
   window opened from the tray -- the four pillars (solar, grid, home,
   battery) around the inverter, an arrow per leg whose direction is the
   current and whose thickness is the watts on one shared scale, watts big
   at every pillar, the secondary numbers small in a monospace face, a liquid
   tank SOC gauge that shows the empty part too, and one line of status.
   The day's figures and the cable bar were under the board and were
   **removed on request (2026-09-20)**: they live in the history window,
   and the arrows carry the limits. The header is the source verdict,
   never the raw mode.
   Under the board, and the one thing added back since (**on request,
   2026-09-22**), is **the day's rule**: a horizontal 24-hour scale
   carrying what the four pillars cannot -- which way the house is
   pointed and when that changes. It runs **sunrise to sunrise** (owner,
   same day), sunrise being the turnaround -- the minute the pack stops
   falling and the sun pushes it up again, which yesterday's lowest SOC
   places from a single point of rise. That is the plan's own day: the
   night is one stretch rather than two halves either side of midnight,
   and both cuts land on the scale instead of off the right edge.
   `policy.window_origin` gives the left edge and everything is drawn as
   minutes from today's midnight, which run negative into yesterday and
   past 1440 into tomorrow. Left of the now marker
   is the record (the `policy.tape` the collector publishes, what the
   inverter was actually set to); right of it the plan, the same colours
   lighter. **A lane is coloured by who carried the house, not by the
   setting** (owner, 2026-09-22): SBU is green on the sun and teal on the
   pack, SUB is orange on the utility and blue when the grid failed and
   the pack carried it anyway. The letters in the lane stay the setting,
   so it says both. Ahead of now there is no source, so the plan wears
   the setting's colour. The source and grid presence ride on the tape,
   settled 5 min (`SETTLE_S`) against the dawn flapping and the clouds,
   and a pack under `BURST_SHARE` of the house with the sun up still
   reads as solar. The palette itself is whose power it is, not what the
   wire is -- green sun, teal pack (yesterday's sun), orange utility,
   violet house -- and it is shared with the history window. The tray
   icon keeps its own ladder, which matches RouterOps. The sun's window
   is a faint wash under all of it.
   The cuts are the owner's two switches, labelled with their times: a
   time the plan has not worked out yet wears a `~` (the latest-release
   clock standing in), one past midnight is labelled at the right edge
   with a `>`, the floor hold fades out rather than claim a clock it does
   not have, and a red underline marks where the inverter disagreed with
   the plan. `policy.lanes_ahead` / `lanes_behind` do the arithmetic,
   doctested; `watch._lanes_shapes` / `_lanes_text` draw it. Keep the
   board itself to the four dimensions -- the rule is a fifth element
   under it, not a fifth pillar in it.
   Every arrow is coloured by its own limit -- grid and home against the
   7/29 (`GRID_LINE_RATING_A`, `LOAD_LINE_RATING_A`), battery against the
   pack (`BATTERY_CONTINUOUS_A` amber, `BATTERY_MAX_A` red) -- and blinks
   at the limit on a 500 ms timer that exists only while a leg is at it.
   The home leg is the sum of both output lines against one line's rating
   because the split is unknown: red means "one line could be at its
   limit", not "both are". The rule lives in `flow.leg_stress` and the tray
   icon uses the same one: a red corner mark only, alternating with the
   plain icon on its own 500 ms timer; digits and bar stay the battery's. GDI+ draws the shapes anti-aliased
   and plain GDI takes over if gdiplus is missing. `--png` renders a frame
   to a file: look at it before and after touching the layout. A LAN web
   dashboard was built and then **removed** on request -- a browser tab costs
   100-300 MB to render 800 bytes of JSON, and the window repaints only on a
   new sample and costs nothing while closed. Do not reintroduce a web view
   without being asked. No balloon notifications either: the grid-lost balloon
   was removed on request (mode flaps around dawn), and `TRAY_NOTIFICATIONS`
   stays False.

   Backward compatibility is a requirement here: `GetDpiForSystem` and
   `SetProcessDpiAwarenessContext` are Windows 10 1607/1703+, so they are
   looked up lazily inside try/except and fall back to `GetDeviceCaps`.
   Never resolve a version-gated export at module import -- a missing one
   raises AttributeError and takes the whole tray down on an older machine.
6. **Usage insights.** `insights.py` reads the logs (never the link) and
   reports per day: energy, peaks, sun hours, battery fullest/lowest, every
   on-battery episode with Wh drawn and SOC cost, marked "by priority" or
   "outage", and an hourly table; across the run, the peak hours and the
   pack capacity the episodes imply. This is the evidence base for the
   battery-backup question and for choosing SBU/USB -- see Oracle #29.
   **Drawn, not only tabled:** `history.py` is the same analysis as a
   native window (tray menu "Power history", or click the day strip on the
   watch screen) with four tabs. DAY: curves -- solar area, house, derived
   grid, SOC on a right axis, a battery strip charging up / discharging
   down, every on-battery episode shaded amber and every outage red -- then
   the facts, the episodes and the hour-by-hour profile. WEEK / MONTH /
   YEAR: solar/house bars per day (per month for the year) with the battery
   share and the 7-day typical line, the typical hour of the range, and the
   review: totals, medians, best/worst days, usage peaks, sun window, when
   the pack is fullest and lowest, episodes by priority vs outage, implied
   pack capacity. **Complete since inception:** every finished day's
   analysis is computed once and cached as `logs/days/<date>.json` (keyed
   on the log's size), so a year is a read of small files, not a parse of
   the JSONL; days before logging began show the inverter's counters.
   Today is live, read incrementally once a minute while open and dropped
   on close. Keys: Left/Right step by the tab's unit, Home, D W M Y; click
   a bar to open that day or month. Subclasses `WatchWindow`.
7. **Daily production.** From the inverter's own `QED` counter, shown on the
   watch screen with yesterday against the 7-day median; a drop past
   `PV_DROP_WARN_FRACTION` says "check the panels".
8. **Automatic output priority.** The owner's rule in `src/policy.py`,
   replacing the inverter's timer. Two walls and a spend between them
   (owner, 2026-09-23): the **evening belongs to the reserve** --
   `AUTO_EVENING_RESERVE_H` (4 h) after dusk the pack is never spent,
   because the outages cluster there and the motors run; after that
   **spending beats idling**, since the number slides either way, so
   release (SBU) at the moment that **lands the pack on the floor exactly
   at the turnaround, nothing left over and nothing short**; and if the
   load would eat into the morning, **stand down** -- back to the grid
   with the night's planned spend done. SBU through the sun's window; SUB
   at the floor (30 %) at any hour until the sun has it past 35 %.

   **The night is settled in SOC points, not watt-hours** -- the fix of
   2026-09-23. Points because the pack's Wh scale is the one figure
   nobody has: `pack_wh` sits pinned at the 2000 Wh cap (20 Wh a point)
   while the logs measure 48-55 Wh a point out, so the watt-hour chain
   thought the pack 2.5x emptier than it is and held the release hours
   too late (it wanted 06:10 where the points said 04:10). The rate is
   `points_per_kwh`, measured off **last night's** episodes -- "last
   night is our light", the same reading the turnaround and the dusk get.

   **Every night patches the next.** The landing's miss against the floor
   is carried as `trim`, in points, half per night, clamped at
   `AUTO_TRIM_MAX_POINTS`, persisted in `logs/trim.json`. Points left
   unspent widen tomorrow's window at the FRONT (release earlier); a
   landing under the floor shortens it at the BACK (stand down before
   dawn), so a correction never eats the evening reserve. The patch is
   sized by the load at the hours it moves, never by the clock. A night
   with an outage over `OUTAGE_SPOILS_S` is not carried -- it spent the
   pack for its own reasons. **`AUTO_RELEASE_LATEST` is now a backstop,
   not a ceiling**: it applies only with no SOC to compute from. It was
   the rule while the night was in watt-hours, and its evidence (the
   "idle slide wastes a held pack" night) was withdrawn by `_labs` --
   the full-to-full balance shows the slide is the number, not energy.
   Turnaround = **hooked to sunrise** (`src/sun.py`, the NOAA equation from
   `SITE_LATITUDE`/`SITE_LONGITUDE`): what is learned from the logs is the
   OFFSET from that day's sunrise, and today's turnaround is today's
   sunrise plus the median of the credible offsets. The season lives in
   the sun, so two days in different months compare directly, the band is
   the reading's own noise alone, and a run of odd days cannot walk the
   figure away from where the sun actually is. Measured here: +52, +53,
   +54 min. The offset is learned, never fixed, and sharpens as days
   accumulate. The raw low still has to survive **two guards** (owner, 2026-09-23):
   it must not be before that day's first PV, because a utility charge
   lifts the pack too and that rise is not a sunrise; and it must be
   within a band the **sun itself** can explain -- `SUN_DRIFT_MIN_PER_DAY`
   (2 min, the ceiling on how far either end of the day moves in 24 h at
   this latitude) times the window, plus `TURNAROUND_NOISE_MIN` (15) for
   the reading, which is a PV-versus-load crossing and moves with cloud,
   load and **panel cleanliness** -- dust drifts it later week by week
   (absorbed, it is gradual) and a wash steps it earlier in one day
   (allowed, it is real). Outside that band it is rejected
   and **the last credible day kept** -- a rainy morning the sun never
   lifts, or a grid charge, moves this further than a day of season ever
   does. Dusk is behind the same
   construction on `DUSK_QUANTUM_MIN` (60), because it is read at hour
   resolution and two days may legitimately differ by a whole step.
   Rejections are listed in `notes.turnaround_dropped` / `notes.dusk_dropped`,
   and **every day's raw reading is kept in `notes.observed`** whether used
   or not -- logged daily with the plan and printed by `ctl.py auto` -- so
   the dust-and-wash pattern is visible rather than thrown away. dusk = the end of the last hour at a
   quarter of the best PV hour, yesterday; pack Wh = the SOC-scale median
   the episodes imply; efficiency and the hourly load from the last 7
   finished days (`src/days.py`, the cache the history window also uses,
   built in a thread once a day). `src/autopilot.py` is the collector
   glue: verify by QPIRI every 5 min, write with readback (an ACK is not
   evidence), 10-minute dwell except the floor, stand down if the owner
   set UTI. `python ctl.py auto` prints the profile, the verdict and the
   release time per SOC with no link. Published as `policy` in
   `logs/state.json`; the watch status line and the tray menu show it.
   **Advisory by default**: `AUTO_PRIORITY = False`. It is its own gate,
   like the clock, because this is the setting the owner already flips
   twice a day; `ALLOW_WRITES` still guards everything else.
   **Since 2026-09-22, for Switch-X** (`D:\PowerTools\switch-x`, which
   reads `logs/state.json` and writes nothing to the inverter): the
   `policy` payload is a contract -- `want`, `phase`, `release_at`,
   `turnaround`, `usable_wh`, `need_wh`, `floor`, `current`, plus
   `until`, `headroom_wh`, `request`, and `tape`, the lane record
   (`{date, marks: [{at, want, current}]}`, one mark per change in the
   plan or in the setting the inverter is actually on, `at` a dated ISO
   minute kept 36 h -- it has to outlive midnight because the rule's
   night does -- and seeded from the last published state so a restart
   keeps the day behind it) -- add keys, never rename. The
   release keeps `AUTO_RELEASE_MARGIN_WH` (300 Wh) above the expected
   draw and publishes the rest as `headroom_wh`, the Wh Switch-X may spend
   on bursts after the release. A request file, `logs/request.json`
   (`{"want": "SUB", "until": "HH:MM", "why": "..."}`, or `ctl.py request`),
   is phase `requested`: ahead of the plan, never past the floor, capped at
   `AUTO_REQUEST_MAX_MIN` (60) from first sight, ignored with the grid
   absent, ended by removing the file; it skips the dwell like the floor.

What the inverter cannot give: a per-line load. It reports one combined
output and carries **no per-line figure at all** -- there is nothing to tell
apart, so the owner's Tuya meters on each line cannot help label it. The
cable-overload indicator is the sum against `LOAD_LINE_RATING_A`. Decided
2026-09-20: no Tuya integration; the meters stay the owner's own readout.

## Layout

| File | What |
|---|---|
| `config.py` | Every site-specific value. `config.local.py` overrides it, gitignored. `ROOT` is the checkout, found from the exe when frozen. |
| `main.py` | The frozen exe's entry point: `tray` or `collector`, dispatched to that script's `main()`. Crash log for the windowed process. |
| `icon.py` | Writes `icon.ico` from the tray's own drawing. Build-time only. |
| `PhantomBridge.spec` | The PyInstaller build: one folder, no console, `icon.ico`. |
| `probe.py` | Read-only recon: find the dongle, report which transport is available. |
| `collector.py` | The resident poller. Redirect, poll, log JSONL, publish `logs/state.json`. |
| `ctl.py` | One-shot CLI: reads, writes, clock, discovery sweep, restore, `auto` (the SUB/SBU plan, no link). |
| `tray.py` | Windows notification-area battery readout. |
| `watch.py` | The watch screen: a GDI/GDI+ window hosted by the tray process. `--png` for a frame. |
| `insights.py` | Usage report from the logs. Opens no link. |
| `history.py` | The power history window: day / week / month / year tabs. A day as curves; a range as bars, the typical hour and the review. Past days cached under `logs/days/`; today read incrementally. `--png` for a frame. |
| `src/frames.py` | CRC, escaping, Eybond framing, dialect detection. Doctested. |
| `src/pi30.py` | Command catalogue + response decoders. Doctested. |
| `src/link.py` | Transport: server/direct/dry-run links, sessions, correlation. |
| `src/bridge.py` | Shared connect/log/poll/clock-sync used by both entry points. |
| `src/flow.py` | Power-flow derivation and the ASCII panel. |
| `src/energy.py` | The day book: inverter counters plus integrated battery/grid Wh. Doctested. |
| `src/days.py` | Every finished day's analysis, parsed once and cached under `logs/days/`; today read incrementally. Shared by history and the autopilot. |
| `src/policy.py` | The SUB/SBU rule: profile from day analyses, need/usable arithmetic, the Governor. Pure, doctested. |
| `src/autopilot.py` | The collector's side of it: daily profile in a thread, QPIRI verify, POP write with readback, the `policy` payload. |
| `src/clock.py` | SNTP client and clock encoding. Doctested. |
| `src/arbiter.py` | Named-mutex link arbitration and the no-spin idle wait. |
| `src/netutil.py` | LAN address / broadcast / firewall helpers. |
| `_labs/` | The battery investigation (Oracle #29): spikes and dated evidence, Switch-X's convention. `pack_audit.py` reads the logs and Switch-X's meters, opens no link. Its README carries the verdicts so far and the tests (T1 hold night, T2 full-drain night, T3 the Pace BMS). May be deleted without warning; what it proves goes to FINDINGS. |

## Conventions

- Python 3.10+, **standard library only**. Still true, including the Win32
  tray — it is ctypes, not pywin32. Keep it that way.
- On this machine `python3` does not exist. Use `python`.
- `config.py` holds every site-specific value. Nothing hardcoded elsewhere.
- `ALLOW_WRITES` stays `False`. It gates power behaviour. The clock is gated
  separately by `ALLOW_CLOCK_WRITES`, which is on, because an RTC cannot make
  units fight or mistreat a battery. The output priority is gated separately
  too, by `AUTO_PRIORITY` (off in `config.py`, flipped in `config.local.py`):
  it is the one setting the owner already changes twice a day, and only
  `POP01`/`POP02` pass through it.
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

**What runs is the exe, not the sources.** Since 2026-09-20 the two
services are one PyInstaller build, the RouterOps way:

```
python icon.py                          # only after changing the tray drawing
pyinstaller PhantomBridge.spec --clean  # -> dist\PhantomBridge\PhantomBridge.exe
dist\PhantomBridge\PhantomBridge.exe tray
dist\PhantomBridge\PhantomBridge.exe collector --quiet --dongle <dongle-ip>
```

`main.py` is the exe's only entry point and dispatches on the first
argument; `dist\PhantomBridge\` **is** the installation, so rebuild after
touching any `.py` -- the sources are not what the Run key starts. One
folder rather than one file because two of these start at every login and
a `--onefile` exe unpacks ~15 MB into `%TEMP%` each time. The exe is not
faster or lighter than `pythonw` (same interpreter, same ~23 MB); it buys
an identity -- its own icon in Explorer, Task Manager and Startup, its own
name, and no dependence on the installed Python. No console, so
`ctl.py`, `insights.py`, `probe.py` and the collector's `--panel` stay
`python <script>` from the checkout, and the frozen tray/collector write
an unhandled exception to `logs/crash.log` instead of a console.

Frozen, `__file__` is inside the bundle, so `config.ROOT` finds the
checkout by walking up from the exe to the first folder holding a
`config.py`; `config.local.py` and `logs/` are read from there, the same
files the source-run CLI uses. A source run (`python tray.py` for a debug
session) does not rewrite a Run value that already points at an existing
exe -- the exe is the installation once it exists.

`icon.ico` is the tray's own drawing (`tray.render_icon`) at a full green
100, written by `icon.py` with no Pillow and no GDI. It is committed so a
clone builds.

Internet is not needed for any of it. Only the clock sync reaches out (SNTP,
3 s timeout per server) and it degrades to a logged miss.

## Safety

- Redirecting the dongle silences the WatchPower app, and on this dongle there
  is no alternative route. Reversible via `python collector.py --restore`.
  Always leave that path working.
- On a parallel stack, never write output/charger priority to one unit alone.
  Mismatched units fight each other. **This site is a single chassis**, so the
  rule does not bite here — but `ctl.py` still enforces it, and that
  enforcement should stay for the next machine.
- The user is a developer (Python, Next.js, PHP). Skip the basics.
