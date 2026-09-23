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

### 2026-09-22 -- the beep at 03:18, and two silent writes at 03:41

The 03:18 `POP01` was followed by one long beep, the kind the owner
knows as the source-interrupt alarm; the timer's own switches (SUB at
5 pm, SBU at 02:00) never beep. Tested at 03:41: `POP02` by hand (line
to inverter, ACK, QPIRI read back SBU) and `POP01` by the collector
twenty seconds later (inverter to line, ACK, QPIRI read back SUB). **Both
silent.** So it is not the parameter write, and not the transfer
direction.

On the bus the 03:18 and 03:42 transfers are identical: QMOD B -> L, grid
steady at 217 V / 50.2 Hz, no QPIWS bit, no `on-battery` disagreement.
The one difference is how long the unit had been in inverter mode before
going back to line: **78 minutes** at 03:18 (since the timer's 02:00
release) against **13 seconds** at 03:42. Working theory: flag `y`
(`alarm_on_primary_source_interrupt`, enabled) sounds on a return to line
after a sustained spell on the pack. The next real switch of that kind
is tomorrow's dusk (`SBU -> SUB` after a day on solar); if it beeps, the
targeted fix is one flag write, `PDy`, and the buzzer stays on for
overload and low battery. Not done: no flag has been written.

`ctl.py set` turned out to have been broken since the first commit
(`precheck_set` called, never defined); fixed today. It cannot open a
link while the collector holds it -- the dongle keeps one session and
the collector's listener has the port -- so a hand write means stopping
the collector, writing, and starting it again.

### 2026-09-22 -- the pack is counted as 2 kWh, not the 4.8 the SOC implies

The owner: the pack was sold as 5 kWh and gives about 2 kWh in practice.
The autopilot's profile had read the pack from the on-battery episodes
(Wh drawn per SOC point) and got **4768 Wh per 100 % over 5 episodes**
-- the sold figure, near enough, because the inverter's voltage-derived
SOC is on the sold scale, not the delivered one. At 38 % that made
381 Wh "above the floor" and a release at 06:22; at 2 kWh it is 160 Wh
and no release tonight. So the derived figure is now capped at
`BATTERY_PACK_WH_CAP = 2000` (the data may say less, never more), the
profile note says both numbers, and the reports compare the implied
figure with `BATTERY_SOLD_WH`. The proper measurement is Oracle #29.

Same day, for Switch-X (which reads `policy` in `state.json`): a release
margin of 300 Wh with `headroom_wh` published; a request file
(`logs/request.json`) the Governor honours as phase `requested`, capped
at 60 minutes, never past the floor, ignored with the grid absent; and
the payload keys declared a contract. Measured cadence of `state.json`
from today's log, 3317 cycles: median 3.1 s, p90 7.1 s, p99 10.2 s; nine
gaps over 12 s, the longest 32 s (the energy back-fill at session start,
a POP write with its readback, and the QPIRI verify all sit inside one
cycle). A reader that calls it stale at 30 s will see a false stale a few
times a day; 60 s is the honest threshold, and the tray uses 120 s.

The menu-99 timer on the panel is **not cleared** (the owner left it as a
backup; `config.local.py` reads QPIRI every 60 s so the collector
re-asserts within a minute plus the dwell). Its 02:00 SBU and the plan
disagree most nights -- tonight's release is later than that at any SOC
under 80 -- so clearing it is the owner's next panel job, filed in
Oracle.

### 2026-09-22 -- the 45-minute hole was a dead loop, not a dead link

Switch-X reported no `state.json` from 01:23 to 02:08 with the collector
alive. The log agrees and says more: the last poll was at **01:23:04**,
the `disconnect` came at **01:23:38** -- 34 s of silence first, which is
the dongle closing a session nobody was talking on -- and then nothing
at all until a fresh session at 02:08:26 with the start-up sequence
(dialect confirm, clock sync). Yesterday's 14:28-14:45 hole has the same
shape. Every other disconnect this week was followed by `poll-failed`
within seconds and a `relink` at the sixth, so the relink path works;
it cannot fire from a loop that is no longer running. The poll loop is a
daemon thread; an unhandled exception in it killed the thread with a
traceback to a console the frozen exe does not have, and the main thread
sat parked, alive, publishing nothing.

What raised it is not in the log, because nothing caught it. The one
thing new on 2026-09-21/22 is a second reader of `state.json`
(Switch-X), and the publish is a rename over the open target, which on
Windows raises `PermissionError` while a reader holds it. Unproven, but
it is the only candidate that arrived with the symptom. Fixed on both
sides: the loop now catches anything, logs `cycle-error` with the trace,
counts it as a failed cycle (so the relink still fires) and carries on;
the rename retries three times and then skips that cycle's publish. The
collector also logs `collector-start` and `collector-stop`, so a hole
with neither is a dead process and a hole with a start after it is a
restart.

The 03:13-03:46 connects with no disconnect rows are **restarts by
hand**: the commits at 03:14, 03:19, 03:31 and 03:43 are that session
rebuilding and restarting the exe (and stopping it for the 03:41 hand
write). A `Stop-Process` logs nothing, which is what the new stop event
is for. 04:44 was this session's rebuild. The link did not drop on its
own tonight; the DHCP reservation stays the fix for the other failure
(the dongle moving), not this one.

### 2026-09-22 -- the first autopilot night: why there was no 06:10 release

The watch screen showed "release ~06:10" in the small hours and the
pack was still on SUB at 06:10. Not a missed write: the plan withdrew
the release, and the number the owner saw came from a build that no
longer runs.

The night, from the log. The panel's menu-99 timer flipped to SBU at
02:00 and the pack ran the house from 63 % to 41 % before
`AUTO_PRIORITY` went on at 03:18 and the first POP01 set SUB (readback
proven). Something put it back to SBU between 03:38 and 03:41 -- the
collector was stopped for a hand write then, so it saw the change only
as a restart's initial read -- and 03:42 set SUB again. From 03:20 the
SOC still slid, 40 % to 34 % by 06:20, on SUB with 1 A of utility
charge showing: the voltage-derived SOC relaxing after the discharge,
not draw.

Where 06:10 came from. The builds of 03:14-04:44 counted the pack as
the episodes' median, 4768 Wh, with no margin. At 39 % that is 429 Wh
above the floor, and the expected draw from 06:10 to the 07:14
turnaround (316 W then 350 W, at 0.78) is about 440 Wh, so the release
sat at 06:10-06:12. The build committed at 05:14 and running since 04:44
caps the pack at 2000 Wh and keeps a 300 Wh release margin; the same
39 % is 180 Wh, under the margin alone, so from 04:44 the decision has
been "too little above the floor to release tonight" and stayed so. At
06:10 the pack read 35 %: 100 Wh usable against 396 Wh of need plus the
margin. On the 2 kWh scale a release needs at least 45 % even at the
end of the night, and with the timer spending the pack from 02:00 that
is not reachable. Clearing menu 99 is what makes the plan's release
possible; the margin is the second lever (`AUTO_RELEASE_MARGIN_WH`).

One quirk fixed: each start logged its first `priority-decision` on the
placeholder profile (fallback pack, default hours) before the plan
thread landed, and never again while the phase held, so the logged
release times (06:34, 06:38, 06:36) were the placeholder's and the real
estimate reached only the screen. The profile is now part of the log
key -- the line is written again when the real one lands, and says
which profile it used (`placeholder` / `3 days, pack 2000 Wh`).
Rebuilt and restarted 06:23.

### 2026-09-22 -- the owner's verdict on the first night: the floor is the reserve

The night above, seen from the owner's side: the plan held a 40 % pack
on the grid from 03:18 to dawn and the pack gave nothing after that. The
panel's timer (SUB 17:00-01:00, SBU from 02:00) would have let it run to
the inverter's own cut-off. Two facts from the owner: the inverter's
cut-off in practice leaves about 10 % undrained, while the plan left 40,
which is what the automation was meant to fix, not worsen; and the SOC
seems to fall slower under load than idle (40 -> 34 on the grid tonight
with a 1 A trickle showing). The idle slide is parked under Oracle #29,
the sold-vs-practical kWh question -- it is the same voltage-derived
scale.

The rule changed, not the arithmetic: `AUTO_RELEASE_LATEST = "02:00"`.
The pack is never held past that clock; the sum can release earlier
(a full pack on a light night) but never later, so the plan is at worst
the timer and the pack runs to the 30 % floor, where the floor rule
takes it back to the grid until the sun lifts it past 35 %. `ctl.py
auto` at 06:36 with the new rule: SBU, "past the latest release 02:00",
80 Wh above the floor. Rebuilt and restarted; the first write under the
new rule is in the log after this entry's time.

### 2026-09-22 -- the floor from the data: the SOC slides 2 points an hour at rest

The owner wants the floor as low as the pack can safely go and asked for
numbers rather than a guess: 30 was chosen because a long outage that
drains to 20 and then an idle spell could take it to the point of no
return, or into the grid charging at 2 A. All four days of QPIGS reads
(36,309 samples, 2026-09-19 22:23 to 2026-09-22 06:48) say this:

**At rest the SOC falls about 2 points an hour, at every level, with the
resting voltage not moving.** Eight idle stretches of 30 minutes or more
(grid carrying the house, no discharge, no real charge):

| start | length | SOC | resting V | slide |
|---|---|---|---|---|
| 09-19 22:50 | 3.2 h | 72 -> 66 | 53.2 -> 53.2 | -1.9 /h |
| 09-20 17:44 | 1.9 h | 84 -> 80 | 53.3 -> 53.3 | -2.1 /h |
| 09-20 19:38 | 1.8 h | 79 -> 76 | 53.3 -> 53.3 | -1.7 /h |
| 09-20 22:19 | 2.7 h | 73 -> 67 | 53.2 -> 53.3 | -2.2 /h |
| 09-21 17:27 | 5.0 h | 84 -> 74 | 53.5 -> 53.3 | -2.0 /h |
| 09-22 03:42 | 2.9 h | 39 -> 34 | 52.6 -> 52.6 | -1.7 /h |

The voltage is flat while the number falls, so this is not the pack
losing charge: the inverter's SOC has a clock in it. It is not a
voltage lookup either (the voltage did not change), and the same 2/h at
84 and at 39 rules out self-discharge of any real size. Call it what it
is: **the SOC figure decays about 2 points an hour whatever the pack is
doing.**

**Under load it falls faster, not slower**, 5 to 10 points an hour at
the night's 250-550 W, and about 52 Wh per point (median of nine
episodes; 48 on the long 02:00-07:19 run of the 21st). But 2 of those
points an hour are the same clock, so at a 250 W night load roughly
**40 % of the SOC points spent are the decay, not energy**, and the Wh
per real point is nearer 75. The owner's impression that it drops more
at idle than under load is wrong as points per hour and right as waste:
idle spends 2 points an hour for nothing.

**The low end is unmeasured below 32.** The lowest ever seen is 32 % at
52.1 V under 8-9 A (this morning); resting voltage is 52.6 V from 34
to 40 and 53.3 V from 63 to 70. The inverter's own protection is
voltage: back-to-utility at 46.0 V, under-voltage at 44.8 V. Nothing in
four days has come within 6 V of it, and a LiFePO4 bus at 52 V under
load is mid-charge, so the SOC scale's "30 %" is a long way above the
pack's real floor. That is also the point: **the floor protects nothing
during an outage** (the inverter runs the pack regardless of SUB/SBU
then, down to 46.0 V); it sets how much is left when one starts, and how
far the decay can run before the sun.

Sizing, from the slide: a floor hit at 04:00 reads about 6 lower by the
07:14 turnaround. 25 reads ~19 at dawn, 20 reads ~14; either is still a
voltage the data has never seen. Set now, in `config.local.py`:
**`AUTO_SOC_FLOOR = 25`, `AUTO_SOC_RESUME = 30`** (the resume only
counts inside the sun's window, so it is "sun is back", not a wobble).
20/30 is the next step once a night at 25 has logged the voltage there.
Collector restarted 06:49; the pack was at 32 % on SBU and is running
down to the new floor.

Worth building next, because it is the guard the owner is actually
describing: a **voltage floor** in the policy (SUB when the battery
voltage under load falls under a set level, say 50 V), so the pack's
real state, not the decaying number, is what stops the drain.

### 2026-09-22 -- the battery investigation opens: what four days of logs settle, and what they cannot

Oracle #29 became the next job (owner, 06:55): sold 5 kWh, delivers ~2,
the SOC slides at idle -- cells, BMS, inverter or settings? `_labs/` is
the investigation folder (Switch-X's convention) and `_labs/pack_audit.py`
is the first instrument: every QPIGS sample logged (41,494, 09-19 22:23
to 09-22 13:39) joined to Switch-X's inverter_in / out_1 / out_2 meters.
The dated output sits beside it. No link opened.

**Settled by the logs**

- *The pack is charged full every sunny morning.* 57.6-57.7 V by
  10:40-11:08 three days running, charge current 0 at float with sun to
  spare, held there till ~14:40-14:54. Bulk = float = 57.6 V (3.60 V/cell
  on 16), 40 A, solar only, type 9. The top of the charge is not the
  problem.
- *The pack absorbs 2.7-3.1 kWh every morning* from the night's low
  (40, 37, 29 %) to 100: 44 Wh per SOC point at the terminals, 50-57 Ah.
  The charge figure is net battery current (in B mode, PV = load + V x A
  within 6 %; the two Switch-X output meters agree with the inverter's
  own load figure within 5-8 %). So the pack holds **at least ~2.5 kWh
  above the 30-40 % state**, and is not a 2 kWh pack in any physical
  sense. What lies under 29 % is unmeasured: no run has gone there, and
  46.0 V has never been within 6 V.
- *The idle slide is the number.* The 2 points/hour at rest (seven
  stretches, -1.7 to -2.2) happen with a flat resting voltage, but on a
  LiFePO4 plateau that alone proves little (53.3 V rest spans 60-84 % on
  the map, 52.6 V spans 30-44 %). What proves it: the 03:42-06:20 stretch
  this morning fell 39 -> 34 with **1.00 A mean flowing INTO the pack** on
  the inverter's own sensor, and the slide starts at 14:40-14:54 with the
  pack still at 57.6 V float and the charger idle. A real 2 pt/h drain
  would be 1.6-1.9 A out; the sensor reads 0.
- *The SOC is not a coulomb counter either.* 44 Wh per point in on the
  charge side, 48-55 out on the discharge side (median 54 raw; 77 net of
  the slide). More energy out per point than in per point is impossible
  for a counter. It is the inverter's estimate with a clock in it, clamped
  at 100; its scale is unknown and `BATTERY_PACK_WH_CAP` stays.
- *The inverter's own cost.* On the pack at the night's 200-450 W the
  output meters read 75-80 % of V x A out of the battery: a fixed 50-100 W
  plus 10-15 %. At float the pack shows "2 A out" for hours with the
  charger idle -- the inverter's DC-side draw off the battery terminal
  while the SCC tracks the bus, or a sensor offset; ~110 W either way.
- *The Inverter In meter is the wrong instrument.* At night in bypass it
  reads 30-40 % under out_1 + out_2 with an apparent power factor of
  0.31; a bypassing inverter cannot take fewer real watts than it puts
  out. Clamp meters mis-read distorted current. Not usable for the
  standby draw; the two Breaker-type output meters are good.

So the 5 kWh label loses ~30 % of its scale under a floor nobody has
crossed, 20-25 % of what does leave the pack to the inverter at light
loads, and the owner's sense of it to a number that counts down on its
own. Whether the cells hold 5 or 4 or 3 kWh the inverter cannot say.

*Same evening, the hold night withdrawn.* The owner: the menu-99 timer
flips to SBU at 02:00, so a hold night is not clean. It is also not
needed. Between one real full and the next, in x efficiency = out +
hidden drain, whatever the number did: 09-20 11:08 to 09-21 10:40, in
3343 Wh, out 2941, hidden drain at most 67 Wh (534 if the float "2 A"
is an offset); 09-21 10:40 to 09-22 10:45, in 3873, out 3170, at most
315 (758). A real 2 pt/h slide would need ~1.7 kWh a day. **The idle
slide is the number, settled twice over** (part I of the audit, and the
1 A-in-while-falling stretch).

**Not settled, and the tests that would** (details in `_labs/README.md`):
T2, a full-drain night -- `AUTO_RELEASE_LATEST = "18:00"` (after dusk,
so the same evening; the timer's 17:00 SUB is overwritten within the
verify minute), floor 0, resume 5, grid present as the net -- V x A out
from 18:00 to the 46.0 V switch is the backup figure at this inverter's
losses, and the voltage curve below 29 % the 20/30 floor step waits for.
Floor 0 reaches 2.875 V/cell, inside LiFePO4's window and above a Pace
BMS's own cut; one full-depth cycle is how capacity is rated. T3, the
Pace BMS once the extender puts it in range -- full-charge vs design
capacity, cycle count, per-cell voltages, current at 10 mA at rest;
`probe.py`'s UDP 58899 sweep first, since an Eybond logger on the BMS
would ride the same redirect and framing. The config night is the
owner's call; nothing was changed tonight.

## 2026-09-24 -- the night beep, late replies taken for the next command's, and a clock that loses minutes in jumps

A review of the build against five days of logs (09-19 22:23 to 09-24 02:00).

### The beep in the night is the solar charger waking on a phantom PV voltage

The owner hears the inverter's own source-change beep -- the one it gives for
an outage or the sun coming and going -- once a night, and did at 01:47
tonight. The logs have it on every night since 09-21:

| night | beep | PV input before -> after |
|---|---|---|
| 09-21 | 01:00:37 | 60.4 V -> 48.7 V |
| 09-22 | 03:18:59 | 75.4 V -> 47.8 V |
| 09-23 | 02:31:40 | 60.4 V -> 49.2 V |
| 09-24 | 01:47:51 | 60.2 V -> 47.9 V |

`QPIWS` bit a0 is a **no-PV flag**, not a reserved or fault bit: it sets every
evening at 18:40-18:43 as the sun goes (PV ~39 V) and clears every morning
when it comes. In **line mode** at night the PV input creeps up with the
panels dark and 0.0 A flowing -- 38, 45, 49, 53, 59 V over a few hours -- and
reads 0 V whenever the inverter stage runs from the pack. At about 60.4 V,
seven above the pack, the charger takes it for the sun: status bit
`charging_scc` on, a0 cleared, the input pulled down to ~48 V, and the beep.
It then stays "charging" until the real sunrise. Harmless: no transfer, no
change to the house.

Two corrections follow. **The 09-22 03:18 beep was this**, not the POP01
write: the write moved the unit to line mode at 03:18:43, the PV input jumped
from 50 to 75 V, and the charger woke 15 s later. The two writes at 03:41-42
were silent because it was already awake. The flag-`y` theory and the `PDy`
fix in that entry are withdrawn. And after the wake the inverter reports
**1-2 A into the pack until dawn with ~5 W on the PV input** (tonight:
+106 W "charging" at 02:12). The "1 A into the pack while the SOC fell"
(09-22 03:42-06:20, in the battery-investigation entry) was that state, so it
is not evidence of anything by itself; the full-to-full balance, the other
proof of the idle slide, only tightens without it. Whether those amps are
real is open: they match the 2 A utility-charge cap, but the AC-charging bit
stays 0 and the charger is set to solar only.

### Late replies were taken for the next command's

76 `tid-mismatch` rows in five days, and every one is the same thing: a
command timed out, its answer arrived while the next command waited, and was
returned as that command's reply. Logged, not enforced -- and decoded. The
logs hold a QPIGS line decoded as QPIWS warning bits, the letter `B` decoded
as a QPIGS sample with every field None, and a warning bitfield stored as a
day's load counter (09-19, repaired by the next minute's re-read). A None
sample reads as grid 0 V, so **6 of 09-22's 10 "outages" and 3 of 09-23's 10
were artefacts** -- the red bands in the history window included them.

The dongle echoes the transaction id faithfully, so the fix is at the
source: a reply with another id is dropped and the wait goes on (logged
`tid-mismatch`, `action: dropped`, with its text). Behind it, `pi30.plausible`
refuses a reply of the wrong shape for its command before it is decoded.
The day readers apply the same check to old logs; the day caches are rebuilt
once (`CACHE_VERSION` 2).

### The RTC loses minutes in jumps, not at a rate

Every clock check so far: -46 s flat from 03:13 to 06:36 on 09-22, then
-217 s at 06:49; -6 s flat from 22:50 to 01:12, then **-751 s** at the next
daily check (09-24 01:12). A daily check left it up to 12 minutes off for the
menu-99 timer and the day counters. `CLOCK_RESYNC_HOURS` is now 1: a QT read
and an SNTP query an hour, a DAT only past the 60 s tolerance, and an hourly
record of when the jumps happen.

### The nightly trim had never run

No `logs/trim.json`, and no `priority-trim` row. The outage guard read the
whole calendar day, so the landing of the 23rd was cancelled by that
evening's 21:18 outage, which came after it. The guard now counts the night
the landing measures: the evening before from dusk, and the morning up to the
landing. Last night is still not carried, rightly -- 52 and 39 minutes out on
the evening of the 22nd -- and a night not carried is now logged, with why.

### Smaller

- `ctl.py` did not claim the link mutex. Windows lets a second listener bind
  8899 beside the collector (checked), so it could jostle the live session;
  it now refuses in 2 s with what to do instead.
- Yesterday's energy counters were re-read every minute all day, 2,880 reads
  a day; now only through the first hour after midnight.
- 40 MB of log a day, 15.6:1 compressible. Days older than yesterday are
  archived as `<date>.jsonl.gz` by the collector once their cache is
  current, verified byte for byte before the plain file goes; query frames
  (`tx` rows, a quarter of each day) are no longer logged, writes still are.

## Next entry goes here
