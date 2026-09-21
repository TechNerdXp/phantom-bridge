# Findings log

Append-only. Date each entry.

---

## 2026-09-19 -- Site baseline (from WatchPower screenshot)

**Inverter:** Long Life Phantom II, 6 kW class, 48 V battery bus.
Labelled "VM4 6000 twin" by the owner.

**Wi-Fi module:** PN `W00344XXXXXXXX`, firmware `3.6.6.6`, status Online.
The "Wi-Fi Module Information" tab plus the PN-as-SSID SoftAP pairing flow
identify this as an Eybond/Shinemonitor-family collector -> UDP 58899
`set>...;` config channel should be present.

**Live snapshot at 09:44:**

| Reading | Value |
|---|---|
| Mode | Line Mode |
| Grid | 235.9 V, 50.4 Hz |
| Output | 235.9 V, 405 W, 6.0% load |
| PV | 0.0 V, 0.0 W |
| Battery | 53.3 V, 75%, charging |

### Open questions

1. **One chassis or two?** 405 W at 6.0% implies the app is sizing against a
   single ~6-7 kVA unit, and the flow diagram shows one inverter icon. If the
   "twin" is genuinely two chassis in parallel, WatchPower is showing the
   master only -- which is its own argument for going local.
   -> Resolve with `QPIRI` (rated VA) and whether `QPGS1` answers.

2. **PV at 0.0 V.** Zero watts is weather; zero *volts* is an open circuit.
   If that 09:44 was AM, the DC side is disconnected -- isolator, breaker or
   fuse. If PM, it is just night. Local timezone puts the screenshot near
   21:00, so **probably night and benign**, but worth one look at the DC
   isolator.

3. **Is the 75% SOC real?** Only if there is closed-loop BMS comms. Otherwise
   it is voltage-derived and unreliable. `QPIGS` exposes the raw battery
   voltage and charge/discharge current -- use those.

### Decisions

- Go local (Door A) rather than cloud API (Door B). The cloud path is
  rate-limited, outage-prone, and its control-field list has no clock entry.
- Keep the vendor cloud endpoint restorable at all times.
- Reads first. `ALLOW_WRITES` stays off until telemetry is boring.

---

## 2026-09-19 (evening) -- First contact. The spike works.

Everything below is measured, not inferred. The dongle answered on the first
attempt and every downstream assumption in this repo was either confirmed or
corrected in one session.

### The link

| Question | Answer |
|---|---|
| Dongle on UDP 58899? | **Yes.** `<dongle-ip>` replies `rsp>server=1;` |
| Redirect works? | **Yes.** `set>server=<ip>:8899;` and it opens TCP to us in ~2 s |
| Eybond framing? | **Confirmed.** Dialect `tail`: wire_len = payload + 2 |
| devcode `0x0993`, FC=4? | **Confirmed.** Every reply came back CRC-valid |
| Dongle's own TCP port? | **No.** 8899, 502, 80, 23, 18899 all closed -- the redirect is the only way in |
| FC=1 heartbeats? | **None sent.** Six sessions, zero heartbeats. That path is moot |

So the 8-byte header reconstruction in `src/frames.py` was right, including the
length rule that was flagged as the weakest guess. `detect_dialects()` picked
`tail` unprompted from the first valid reply.

### The clock -- goal #1, and the assumption that was wrong

**`DAT` works on this unit.** The repo said to expect an `ACK` with a clock
that never moves. Measured:

```
before : 2026-09-19 22:09:58   (inverter is 15m 28s behind)
sent   : DAT260919222526
reply  : ACK
after  : within 1s of internet time -- CONFIRMED by QT readback
```

`QT` is supported and returns `YYYYMMDDHHMMSS`, which is what made the proof
possible: the ACK was not taken as evidence, the readback was. The RTC had
drifted a quarter of an hour, so this was worth doing rather than cosmetic.

Clock writes are now gated separately from everything else
(`ALLOW_CLOCK_WRITES`), because the RTC cannot make units fight or mistreat a
battery and should not sit behind the same gate as charge voltages.

### Identification

```
QPI    PI30
QID    <inverter-serial>
QSID   <extended-serial>
QVFW   VERFW:00060.10
QPIRI  220.0 27.2 220.0 50.0 27.2 6000 6000 48.0 46.0 44.8 57.6 57.6 9 002 090
       0 1 3 1 10 0 0 54.0 0 1
QET    02392500 Wh generated total     QLT  02970600 Wh load total
```

### Open question 1: one chassis or two? -- SETTLED: ONE

- `QPGS0`, `QPGS1`, `QPGS2`, `QPGS3` all **NAK**.
- `QPIRI` reports `parallel_max_units = 1` and `output_mode = 0` (single).
- Rated 6000 VA / 6000 W, and 423 W read as 7% load, which sizes against one
  6 kVA machine.

The "VM4 6000 twin" is a single 6 kVA chassis. `PARALLEL_COMMANDS` is now empty
and `PARALLEL_CONFIRMED = False`. The parallel-safety rule stays in the code for
correctness, but it does not apply to this site.

### Open question 2: PV at 0.0 V -- still open, but now for a reason

Still 0.0 V and 0.0 A at 22:26 local, which is night, so it is expected and
benign. **This cannot be resolved after dark.** Re-read `QPIGS` in daylight;
if `pv_v` is still 0.0 V with sun on the array, the DC side is open at the
isolator, breaker or fuse.

Worth knowing when that check happens: `QPIRI` says **charger source priority
= 3 (Solar only)** and **max utility charge current = 2 A**. So the battery is
configured to charge from PV essentially alone. With the PV side reading zero,
the pack is not being charged from anything. It sat at 53.30 V with 0 A charge
current for the whole session. That is a real operational finding, not a
protocol one -- it is the owner's call what to do about it, and nothing here
has been changed.

### Open question 3: is the 75% SOC real? -- still open

`QPIGS` reported 73%. There is still no evidence of closed-loop BMS comms, so
this stays voltage-derived. `TRUST_SOC` remains `False`. Battery type reads as
code **9**, outside the documented 0-3 range, which is usually a lithium
profile -- suggestive, not evidence.

### Corrections forced by the hardware

1. **`QPIGS` returns 24 fields, not 21.** The documented 21 map aligned exactly;
   the tail carries three more: `0 01 0000`. Mapped as the grid-feed extension
   (`solar_feed_to_grid`, `country_code`, `solar_feed_w`) -- names probable,
   not settled, since none of them has been made to move yet.
2. **`QPIWS` bit a0 is set on a healthy unit.** The spec reads a0 as
   "1 = fault", but this unit reports `1000...0` while running normally in Line
   mode with no other bit set. a0 is now reported raw and never treated as a
   fault; the named bits are the truth.
3. **Battery type 9** is outside the documented range and is now surfaced as
   `unmapped code 9` instead of vanishing as `None`.

### Command support on this firmware

**Answers:** QPI, QID, QSID, QVFW, QMOD, QPIGS, QPIRI, QPIWS, QFLAG, QDI,
QMCHGCR, QMUCHGCR, QBEQI, QT, QET, QLT

**NAKs:** QVFW2, QOPM, QBOOT, QPGS0-3

`QFLAG` returns `EaxyzDbdjklnuv` -- letters `d`, `l` and `n` are not in the
documented flag set and are reported as `unknown:` rather than guessed at.

### Decisions

- Redirect is the only route in, so WatchPower **does** go quiet while we hold
  the link. `python collector.py --restore` gives it back and was exercised.
- Clock sync is solved and proven. It runs on connect and every 24 h.
- `ALLOW_WRITES` stays `False`. Reads are now boring, but nothing here needs a
  power-behaviour write, so the gate stays shut until something does.

---

## 2026-09-19 (late) -- Going live, and why WatchPower cannot share

Checked on this PC: **no WatchPower process and no install directory.** The
app in use is the **Android** one, which reads the vendor cloud rather than
the inverter.

That closes the "can both use it at once" question in two steps:

1. **No.** The dongle keeps one server session and has no listening TCP port
   of its own, so it points at the cloud or at us, never both.
2. **And it cannot alternate automatically**, because the app never touches
   the dongle -- there is no local event to detect when it opens. The only
   untried lead is sniffing UDP 58899 for the app's own discovery broadcasts
   on the same WiFi; speculative, not attempted.

Built instead: a timed loan (`ctl.py handover`, or the tray menu). The
collector drops its session, restores the cloud endpoint, and re-takes the
link when the timer expires. The app needs a minute or two of cloud uploads
before it shows anything current, which is why the default is 15 minutes.

**Now running as services**, started at login, redirect held continuously:

| | |
|---|---|
| Collector | `pythonw collector.py --quiet --dongle <dongle-ip>` |
| Tray | `pythonw tray.py` (follow mode, opens no link of its own) |
| Idle cost | ~23 MB each, ~0.03 s CPU per minute each |

First live clock check after the session restarted reported drift **-1 s** --
the `DAT` write from earlier in the evening held.

---


## 2026-09-19 (housekeeping) -- Identifiers masked for publication

This log is append-only, so this is recorded rather than quietly done: every
device identifier above has been replaced with a placeholder so the repository
can be public.

| Masked as | What it was |
|---|---|
| `W00344XXXXXXXX` | Wi-Fi module part number |
| `<inverter-serial>` | `QID` reply |
| `<extended-serial>` | `QSID` reply |
| `<dongle-ip>` | the dongle's LAN address |

The real values are in `config.local.py`, which is gitignored. Nothing
technical was removed -- field maps, protocol behaviour, verdicts and the
measurements themselves are unchanged.

None of these is a password. The part number is masked because on this
collector family it is how a device is claimed in the vendor app **and** it is
the SoftAP SSID, so publishing it beside that family's default AP password
(`12345678`) would hand anyone in WiFi range an actionable pair. The serials
are device identifiers used for cloud registration and support.

`logs/` is now ignored in full rather than just `*.jsonl`: the JSONL carries
the serials in its `QID`/`QSID` replies, and the earlier pattern left
`logs/state.json` and `logs/handover.json` committable.

---

## 2026-09-20 -- Energy history answers, a silent five-hour outage, and what the night looks like

### The inverter keeps its own daily counters, and they answer

Probed with the collector stopped, 07:02 local:

```
QET          02392500   lifetime PV, Wh          QLT          02973100   lifetime load, Wh
QED20260920  00000000   today (dawn)             QLD20260920  00001773
QED20260919  00012362   yesterday: 12.36 kWh     QLD20260919  00014864   14.86 kWh
QEM202609    00234900   September so far         QLM202609    00291300
QEY2026      02386900                            QLY2026      02964600
```

All six forms answer. The collector now reads today's pair every minute and
back-fills 30 days once per session into `logs/energy.json`. The counters
begin on **2026-09-04** (everything before reads 0), which is presumably the
commissioning date. Per day since, PV / load in kWh:

| Date | PV | Load | Date | PV | Load |
|---|---|---|---|---|---|
| 09-04 | 4.8 | 10.0 | 09-12 | 15.3 | 19.2 |
| 09-05 | 4.4 | 9.5 | 09-13 | 14.9 | 19.1 |
| 09-06 | 16.3 | 20.6 | 09-14 | 15.5 | 18.9 |
| 09-07 | 17.2 | 20.4 | 09-15 | 15.8 | 19.9 |
| 09-08 | 15.7 | 19.1 | 09-16 | 12.7 | 14.4 |
| 09-09 | 12.5 | 15.3 | 09-17 | 12.9 | 14.7 |
| 09-10 | 14.8 | 16.5 | 09-18 | 9.4 | 12.4 |
| 09-11 | 13.3 | 16.2 | 09-19 | 12.4 | 14.9 |

The last four days run 9-13 kWh against 15-17 kWh the week before. Weather,
the season, or dust -- the counters cannot say which, but this is the number
the "clean the panels" indicator is judged on (yesterday against the 7-day
median, `PV_DROP_WARN_FRACTION` in config).

### The house runs from the battery at night, grid or no grid

State at 02:00 and again at 07:02 local: `QMOD = B`, discharge current 9 A
then 4 A, **grid present at 242-250 V**, SOC 66% falling to 41% by dawn.
`QPIRI` reports output source priority **1**. On this firmware that evidently
behaves as solar-battery-utility: the pack carries the house overnight and the
grid is only taken when the pack reaches the recharge voltage (46.0 V).

That bears directly on the "half the promised backup" complaint: a real
outage starts from whatever the night left in the pack, not from full. The
insights report separates the two kinds of on-battery episode ("by priority"
vs "outage") so the claim can be argued from the log rather than a feeling.
Nothing has been changed; it is the owner's setting.

### The link died at 02:00:24 and stayed dead for five hours

A `QPIWS` timeout at 02:00:20, then `disconnect` from the dongle, then
3,000 failed reads at five-second intervals until the probe above re-sent the
redirect and the dongle came straight back. The re-link logic never fired:
`bridge.read()` swallows a dead session into `ok: false` records, so
`snapshot()` never raised and the failure counter never moved. Fixed -- a
cycle with no QPIGS data now counts as a failure, and the redirect is re-sent
every six. `logs/state.json` was also not rewritten during those hours, so
the tray showed a two-o'clock reading as "stale" instead of nothing.

Also found in the same pass: the 24-hour clock resync was gated on
`ALLOW_WRITES` (False) instead of `ALLOW_CLOCK_WRITES` (True), so it never
ran; and the collector's autostart refresh dropped `--dongle` from the Run
key on every launch. Both fixed.

(Someone clicked **Restore vendor cloud and stop** in the tray at 07:01:52,
just before the probe; the collector was already dead by then so it changed
nothing.)

### Dawn: the mode flaps

07:18-07:19 local, 1.4 kW of load, sun just up: `QMOD` alternated B and L
five times in a minute, 3-6 s in each state, at 51.5-52.5 V. The unit is
hunting between the pack and the grid while PV is not quite enough. The tray
raised a balloon on every flip, so balloons are now off
(`TRAY_NOTIFICATIONS = False`). The `on-battery` verdict itself is unchanged.

### QPIGS field 24 is not solar feed watts

It read `0081` at 02:00 and `0072` at 07:02 with no sun on the array. Whatever
it is, it is not PV export. Renamed `unknown_24` and logged raw. Fields 22 and
23 (`0`, `01`) have still never moved.

---

### "On battery" while on solar: the label was wrong, not the inverter

The watch screen said ON BATTERY all morning with 900 W of sun. Audited
against every logged sample (2,016 across 09-19 and 09-20): 266 samples
today were `QMOD = B` with PV on the array, the pack **charging** at 7 A from
the SCC and the grid present at 232 V. That is not a battery-powered house.

`B` is the vendor's "battery mode", and the name is the whole confusion. On
this family it means the *inverter stage* is making the AC from the DC bus
-- which PV and the battery both feed -- as opposed to `L`, where the load
is bypassed straight from the grid. With output priority SBU the unit lives
in B whenever the sun or the pack can carry the house, so B is the normal
daytime state, not an alarm. The mode says which stage makes the AC; the
currents say who is paying for it.

The `on_battery` verdict (B *and* discharge > 0) was already right and the
energy book's "on battery" time was never inflated -- only the screen's
mode label (`B -> "ON BATTERY"`) and the tray's "On grid, charging" headline
were wrong. `flow.analyse` now emits `source` / `source_label` (ON SOLAR,
SOLAR + BATTERY, ON BATTERY, GRID OUT, ON GRID) and both screens lead with
that; the inverter box shows the mode as "INVERTER" or "GRID BYPASS" with
the raw letter under it. The "mode and current disagree" hint no longer
fires on B-with-sun, which was all day.

### Correction: output source priority is 2 (SBU), not 1

The 09-20 entry above and CLAUDE.md said "output source priority 1".
`QPIRI` field 17 reads **2** on every session (`... 0 2 3 1 10 ...`):
Solar-Battery-Utility. Charger priority is 3 (solar only), as recorded. SBU
is exactly the behaviour observed -- the pack carries the house overnight
with the grid present -- so nothing else in that entry changes.

### The daily counter lags the day

`QED20260920` read `00000000` on all 72 reads up to 07:53 with 370 Wh
already integrated from samples. The inverter's per-day counter is
authoritative for the finished day but is not live, so the screen shows the
larger of the counter and the integrated figure for "today so far".


### 2026-09-20 -- one redirect is not enough after an abrupt session loss

Swapping the pythonw collector for the frozen exe meant killing the running
collector, so the dongle kept a half-open TCP session to a process that was
gone. The new collector sent `set>server=` once on start, got `rsp>server=1;`,
and then waited: three minutes of nothing, with 8899 listening and no
inbound connection. A second, hand-sent redirect brought the dongle in within
8 s. So the acknowledgement means "config stored", not "session moved" -- the
dongle only reconnects once it notices its old session is dead, or is told
again. `bridge.open_link` now waits in slices and re-sends the redirect
between them (`REDIRECT_RETRY_SECONDS`, 30 s); the poll loop already did the
same for a link that dies mid-run. On the next restart the dongle connected
within 20 s.

Also today: the resident pair ships as `dist\PhantomBridge\PhantomBridge.exe`
(PyInstaller, one folder, no console). Two things checked frozen: the arbiter
mutex still refuses a second collector (exit 1, no crash), and the exe finds
the checkout's `config.local.py` and `logs/` from `dist\` two levels down.
Idle cost after the switch: 25 MB collector, 30 MB tray -- what pythonw cost.

### 2026-09-20 -- the dongle moved house and the redirect went to a stranger

The collector had survived main-router restarts and the PC sleeping and
waking all day. At 21:24 the router the dongle hangs off was restarted and
the link died for good: `QMOD: no reply within 6s`, then `disconnect` from
`<dongle-ip>`, then 277 failed cycles with a `relink` every sixth -- 46
re-sent redirects over 28 minutes, none of them answered by a connection.

The redirect was going to the wrong box. That router is the DHCP server;
its restart emptied the lease table, the dongle came back on a **different
address** (.104 -> .102) and another device took the old one. `udp_config`
sent unicast only whenever an IP was known -- the broadcast fan-out was the
*fallback for no IP*, so with `--dongle` on the command line (and
`DONGLE_IP` in `config.local.py`) the relink never left unicast. A
`set>server=?;` by broadcast found the dongle at once (`rsp>server=1;` from
.102), a hand-sent redirect to .102 brought it into the still-listening
collector in 5 s, and the poll recovered after 277 failures. A restart
would not have helped: the Run key carries the stale `--dongle` and
`open_link` unicast the same way.

Why the other restarts did not bite: the main router is not the dongle's
DHCP server, and a sleeping PC changes nothing on the dongle's side -- it
just reconnects to the same address when the listener is back.

Changed: every `set>` (redirect, relink, restore) now goes to the known IP
**and** every broadcast address. `send_redirect` returns who answered, the
collector logs `dongle-moved` and follows the new address for the next
unicast and the exit/handover restore, and on connect it takes the
session's peer as the truth over `--dongle`. One dongle on this LAN, so a
broadcast `set>server=` has exactly one listener; a multi-dongle site would
need to think again. The real fix is on the router: a DHCP reservation for
the dongle's MAC. Not done yet.

### 2026-09-22 -- what the inverter's own SUB/SBU timer does, and the autopilot that replaces it

The output priority is not static: the QPIRI reads across sessions show
it at **SUB (1) at 22:03-22:42** on the 19th and 20th and at **SBU (2) at
02:08, 07:15, 12:13 and 14:45**. That is the inverter's timer (menu 99),
which the owner set to hold the pack in the evening and release it at
02:00. Last night's log (2026-09-21) shows exactly that: one on-battery
episode "by priority" from **02:00 to 07:21, 1348 Wh at 252 W, SOC 65 ->
37**, lowest at 07:14, when the sun took over. It also shows the cost of
SBU in the late afternoon: 16:12-16:54, **490 Wh, 93 -> 82 %**, spent
while the grid was there, an hour before the panels went dark.

Built today, advisory by default: `src/policy.py` (the rule, doctested),
`src/autopilot.py` (the collector glue: daily profile in a thread, verify
by QPIRI, write with readback), `python ctl.py auto` (the plan, no link).
The profile it read from three finished days:

```
turnaround  07:14   lowest SOC 2026-09-21 at 07:14
dusk        17:00   PV under 25% of its best hour after 17:00 on 2026-09-21
pack        4768 Wh per 100% SOC   median of 5 episodes
efficiency  0.78 load Wh per battery Wh   (1475 Wh of load per 1897 Wh drawn)
night load  00h 372W 01h 408W 02h 241W 03h 120W 04h 91W 05h 316W 06h 317W
            17h 525W 18h 404W 19h 378W 20h 483W 21h 555W 22h 373W 23h 395W
```

At 03:11, SOC 41 %, it would have said **SUB, hold, release ~06:01**
(1161 Wh needed to the turnaround, 524 Wh above the floor) -- where the
timer had already released the pack at 02:00. From 60 % up it would
release at once at that hour; from 100 % at dusk, the release lands
around midnight. The pack figure is SOC-scale, from the episodes, and the
SOC is voltage-derived: the number is consistent with the floor and the
release, which are measured in SOC points, not with the nameplate.

**Switched on the same night, 03:18.** `AUTO_PRIORITY = True` in
`config.local.py`, collector restarted. The first power-behaviour write
this project has ever sent, and it was proven:

```
03:18:39  before SBU   sent POP01   reply ACK   after SUB   (QPIRI readback)
03:19:34  QMOD L, source grid, 316 W derived grid, 0 A discharge, SOC 40
```

The house moved to the grid within the minute; the pack (40 %) is held
for a release around 06:10. The menu-99 timer is still set on the panel,
so until it is cleared the two may disagree at the timer's own switch
times; the collector re-reads QPIRI every five minutes and re-asserts,
and each such flip is logged as `priority-external-change`.

## Next entry goes here
