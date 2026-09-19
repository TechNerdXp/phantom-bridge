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

## Next entry goes here
