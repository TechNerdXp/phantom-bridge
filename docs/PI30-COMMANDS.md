# PI30 command reference

Scoped to this project. Every command goes out wrapped by `frames.pi30()`
(CRC16-XMODEM + escaping + CR) inside an FC=4 Eybond frame.

**Support column verified against this unit on 2026-09-19.** "NAK" means the
inverter answered and refused, not that the link failed.

## Reads

| Command | Support | Returns | Why we care |
|---|---|---|---|
| `QPI` | yes | `PI30` | Confirms the family |
| `QID` | yes | `<inverter-serial>` | Serial |
| `QSID` | yes | Extended serial | |
| `QVFW` | yes | `VERFW:00060.10` | Main CPU firmware |
| `QVFW2` | **NAK** | — | No secondary CPU reported |
| `QMOD` | yes | One char | **On-battery detection.** `L`=Line, `B`=Battery, `P`=Power on, `S`=Standby, `F`=Fault, `H`=Power saving |
| `QPIGS` | yes | **24 fields** | **Load monitoring.** See below |
| `QPIRI` | yes | Rated values | Settles ratings, priorities and setpoints |
| `QPIWS` | yes | Warning bitfield | See the a0 caveat below |
| `QFLAG` | yes | `EaxyzDbdjklnuv` | Feature flags |
| `QDI` | yes | Factory defaults | |
| `QMCHGCR` | yes | `010 … 120` | Selectable total charge currents |
| `QMUCHGCR` | yes | `002 010 … 100` | Selectable utility charge currents |
| `QBEQI` | yes | Equalization state | |
| `QT` | yes | `YYYYMMDDHHMMSS` | **The clock readback that proves `DAT`** |
| `QET` / `QLT` | yes | Wh totals | Lifetime generated / load |
| `QED<yyyymmdd>` / `QLD<yyyymmdd>` | yes | Wh for one day | **Daily production and use.** Counters start 2026-09-04 |
| `QEM<yyyymm>` / `QLM<yyyymm>` | yes | Wh for one month | |
| `QEY<yyyy>` / `QLY<yyyy>` | yes | Wh for one year | |
| `QOPM` | **NAK** | — | |
| `QBOOT` | **NAK** | — | |
| `QPGS0`–`QPGS3` | **NAK** | — | Single chassis; see below |

### QPIGS has 24 fields, not 21

The documented 21-field map aligned exactly, and this firmware appends three
more (`0 01 0000`). The first two are mapped as the grid-feed extension —
`solar_feed_to_grid`, `country_code` — and have never moved. The third was
first taken for solar feed watts and then seen at 81 and 72 in the dark
(2026-09-20), so it is not that; it is carried as `unknown_24` until it is
understood.

### QPIWS bit a0 is not a fault flag here

The spec reads a0 as "1 = fault, 0 = warning". This unit reports
`1000…0` while running normally in Line mode with every other bit clear. So a0
is reported raw (`a0_set`) and never treated as a fault. The named bits are
the truth.

### Reading on-battery correctly

Do **not** trust the SOC percentage unless there is closed-loop BMS comms.
Without it, that number is a voltage-derived estimate, and on a flat LiFePO4
curve it is close to meaningless. This unit reports battery type code **9**,
outside the documented 0–3 range — suggestive of a lithium profile, not
evidence of BMS comms.

Ground truth is two fields:

- `QMOD` == `B` -> running from battery
- `QPIGS` battery **discharge current** > 0 -> actually drawing down

Both together, not either alone. `src/flow.py` enforces exactly that and
flags disagreement between them as transitional.

### There is no grid-power field

PI30 does not report grid power. `src/flow.py` derives it from the energy
balance:

```
pv + grid + discharge = load + charge + losses
grid = load + charge - pv - discharge      (floored at zero)
```

It is labelled `derived` in the output and in the panel so it is never
mistaken for a measurement.

## Writes (`ALLOW_WRITES` gate)

| Command | Control name | Effect |
|---|---|---|
| `DAT<YYMMDDHHMMSS>` | `datetime` | **Set clock. Confirmed working** — see below |
| `POP00/01/02` | `output-priority` | Output source priority (UTI / SOL / SBU) |
| `PCP00`..`PCP03` | `charger-priority` | Charger source priority |
| `F50` / `F60` | `output-freq` | Output frequency |
| `PGR00/01` | `grid-range` | Appliance / UPS input range |
| `PBT00/01/02` | `battery-type` | Battery type |
| `PBCV<nn.n>` | `recharge-voltage` | Back-to-utility voltage |
| `PBDV<nn.n>` | `redischarge-voltage` | Back-to-battery voltage |
| `PSDV<nn.n>` | `cutoff-voltage` | Low-voltage shutdown |
| `PCVV<nn.n>` | `bulk-voltage` | Bulk / C.V. charge target |
| `PBFT<nn.n>` | `float-voltage` | Float voltage |
| `MNCHGC<m><nnn>` | `max-charge-current` | Max total charge current |
| `MUCHGC<m><nn>` | `max-utility-charge-current` | Max utility charge current |
| `PE<x>` / `PD<x>` | `enable` / `disable` | Feature flags |
| `POPM<m><nn>` | `output-mode` | Single / parallel / phase |
| `PPVOKC<n>` | `pv-ok-condition` | PV-OK condition |
| `PSPB<n>` | `solar-power-balance` | Solar power balance |
| `PBEQE/A/T/P/V/OT` | `equalize*` | Equalization settings |
| `PF` | `factory-reset` | **Wipes every setting above** |

Responses are `(ACK` or `(NAK`.

None of these has been sent to this unit. `ALLOW_WRITES` is still `False`;
only the clock has been written.

## The clock — the assumption that was wrong

This file used to say `DAT` is absent from the published Axpert PI30 spec and
is documented elsewhere to return `ACK` while the clock never moves. On **this**
firmware that is false. Measured 2026-09-19:

```
QT before : 2026-09-19 22:09:58     (15m 28s behind internet time)
sent      : DAT260919222526
reply     : ACK
QT after  : within 1s of internet time
```

The reason the claim can be settled at all is that `QT` works here, so the
`ACK` is never taken as evidence — `ctl.py time --sync` always writes, waits,
reads back and compares. If a future firmware does lie, that check catches it
and reports `ack-ignored` rather than success.

The clock still is not load-bearing: `logs/` is stamped from NTP on our side,
so the data is correctly timed whatever the RTC believes. But the RTC drives
the inverter's own logs, and a quarter-hour of drift was worth fixing.
