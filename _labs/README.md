# _labs -- the battery investigation (Oracle #29)

Spikes and evidence for one question, borrowed as a folder convention from
Switch-X: **why does a pack sold as 5 kWh give about 2 kWh of backup, and
is the "idle drainage" on the panel real?** Suspects, in the owner's words
(2026-09-22): the cells, the BMS (a Pace-type unit), the inverter, or its
settings. Everything here may be deleted without warning; what it proves
goes to `docs/FINDINGS.md` (dated) and what it changes goes to the code.

Nothing in this folder opens the link. `pack_audit.py` reads the logs of
this checkout and Switch-X's line meters (`D:\PowerTools\switch-x\logs\power`);
its output is kept next to it, dated, so the findings stay checkable.

    python _labs/pack_audit.py               # all parts, A..H
    python _labs/pack_audit.py --part BG     # just the idle slide and the refill
    python _labs/pack_audit.py --days 3      # the last three log days

## What the logs say already (2026-09-22, four days, 41k samples)

Read `pack_audit-2026-09-22.txt` for the numbers. The shape:

1. **The pack reaches a real full every sunny morning.** 57.6-57.7 V by
   10:40-11:08, the charge current falls to 0 with sun to spare, and it is
   held at float until mid-afternoon. The charge settings are not starving
   it (bulk = float = 57.6 V = 3.60 V/cell on 16 cells, max 40 A, solar only).
2. **The pack absorbs 2.7-3.1 kWh every morning** going from the night's
   low (29-40 %) to full: 44 Wh per SOC point at the terminals, about 50 Ah.
   That energy is measured as net battery current (PV = load + V x A within
   6 %), so the pack physically holds **at least ~2.5 kWh above the 30-40 %
   state** -- it cannot be a 2 kWh pack in the physical sense. Under that
   state nothing is known: no run has ever gone below 29 %, and the
   inverter's 46.0 V cut-off has never been within 6 V.
3. **The idle slide is the number, not the pack.** At rest the SOC falls
   ~2 points an hour at every level with the resting voltage flat. The flat
   voltage alone proves little on a LiFePO4 plateau (53.3 V rest covers
   60-84 % on the map, 52.6 V covers 30-44 %). What proves it: on
   2026-09-22 03:42-06:20 the SOC fell 39 -> 34 while the inverter's own
   sensor reported **1 A flowing INTO the pack** the whole time. A pack
   does not lose charge while being charged. And the slide starts while
   the pack is still at 57.6 V float with the charger idle (part H).
   A real drain of 2 points/h would be 1.6-1.9 A out; the sensor reads 0.
4. **The SOC is not a coulomb counter either.** Charge side: 44 Wh per
   point in. Discharge side: 48-55 Wh per point out (69-78 net of the
   slide). Energy out per point exceeding energy in per point is
   impossible for a counter; it is the inverter's own estimate with a
   clock in it, clamped at 100. Treat its scale as unknown.
5. **The inverter eats a fixed 50-100 W plus ~10-15 %.** On the pack at
   the night's 200-450 W the two output meters read 75-80 % of V x A out
   of the battery (part E2). At float in the afternoon the pack shows
   "2 A out" (~110 W) for hours with the charger idle: the inverter's own
   DC-side draw, or a sensor offset (part E4). The "Inverter In" clamp
   meter reads 30-40 % UNDER the two output meters at night with an
   apparent power factor of 0.31 -- it under-reads the inverter's
   distorted input current and is **not usable** for the grid-side
   standby draw until checked against a known load.

So of a 5 kWh label: ~30 % of the scale sits under the floor and has never
been used; 20-25 % of what leaves the pack is lost in the inverter at the
night's light loads; the number on the panel slides 2/h on its own,
which makes the night's arithmetic (and the owner) think the pack is
emptier than it is. Whether the cells hold 5 kWh or 4 or 3 is the part
the inverter cannot tell. That needs the tests below.

## The tests

Each one discriminates; none is a fishing trip. T1 and T2 cost one night
of `config.local.py` and no hardware. T3 is the extender and the BMS.

### T1 -- the hold night: is the slide energy?

One night with the pack parked on the grid from dusk to dawn: in
`config.local.py` set `AUTO_RELEASE_LATEST = "07:00"` for that night
(the panel's menu-99 timer will flip to SBU at 02:00; the collector puts
SUB back within `AUTO_VERIFY_INTERVAL_S`, one minute on the pack, fine).
Expected: SOC ~83 at dusk, ~57 at dawn by the clock, resting voltage
unchanged. **The verdict is the next morning's refill** (part G): if the
26 lost points were energy, the refill takes 26 x 44 = ~1.1 kWh; if they
were the number, it takes a few hundred Wh (the float top-up and the
inverter's own draw). Over 800 Wh = real drain; under 300 = the number.

### T2 -- the full-drain night: the backup figure, once and for all

One night from a real full to the inverter's own cut-off, grid present as
the safety net: `AUTO_SOC_FLOOR = 0` and `AUTO_SOC_RESUME = 5` in
`config.local.py` for that night (the 02:00 release stands). The house
runs from the pack until back-to-utility at 46.0 V, then the grid takes
over seamlessly; `batt_redischarge_v = 54.0` keeps it on the grid until
the morning charge. Integrate V x A out of the pack from full to the
switch (part D does this per run; the night is one run) -- **that number
is the backup the pack delivers from full**, at this inverter's losses,
and the rest-voltage / loaded-voltage curve below 29 % that the 20/30
floor step is waiting for. Switch-X sheds loads at 30 and 20 % on the
pack, so the night's draw will be lighter than usual; the Wh count is
still the Wh count. One deep cycle to 2.875 V/cell is inside LiFePO4's
normal window; the BMS's own under-voltage cut sits below it. Do not run
T1 and T2 on the same night, and run T2 after a full sunny day.

### T3 -- the Pace BMS: cells, BMS, or neither

The owner is installing a WiFi extender so the pack's BMS module is in
range. What it settles that nothing above can: design capacity vs
full-charge capacity vs remaining (the pack's real Ah, and how much it
has faded), cycle count, every cell's voltage at full and at the bottom
(a weak cell caps the pack at the BMS's cell limits, whatever the other
15 hold), the BMS's own SOC against the inverter's, its current at 10 mA
resolution at rest (the drain question in one reading), and its
protection thresholds (a BMS that cuts charge at 3.55 V/cell or discharge
at 3.0 V/cell explains a "small" pack with good cells).

First find out what the module speaks; the extender only matters if it
is WiFi at all:

- **Eybond/Shinemonitor datalogger** (the same family as the inverter's
  dongle, and common on packs sold with these inverters): `python probe.py`
  sweeps UDP 58899 and lists every one on the LAN. If it answers, the
  `set>server=` redirect and the 8-byte passthrough in `src/frames.py`
  carry the BMS's RS-485 unchanged; only the payload differs (Pace P16
  ASCII frames, `~25xx46...` with a length checksum and a frame checksum).
  A second collector, not this one -- the dongle keeps one session.
- **Tuya-style module**: Switch-X's `scan` finds it; its DPs would carry
  SOC, voltage, current, temperature, and the resident already logs those.
- **Bluetooth** (what most Pace BMS phone apps use): an extender changes
  nothing; the path is a BT serial link from this PC, and the protocol is
  the same Pace ASCII over the BT SPP.

Reach the module, log a day of its readings beside `logs/<date>.jsonl`,
and part G re-run against the BMS's Ah instead of the inverter's SOC is
the answer to Oracle #29.

### T4 -- the inverter's own draw on the grid (optional)

Only if the standby figure matters for the plan: put a "Breaker"-type
meter (the Inverter Out 2 kind, which agrees with the inverter within
5-8 %) on the inverter's input for a night, or read `wapda_main` minus
the lines that do not pass through the inverter. The clamp meter now on
the input is the wrong instrument for a distorted current.

## What not to conclude

- Not "the pack is fine": part 2 bounds it from below (>= ~2.5 kWh above
  30-40 %), not from above. The label may still be wrong.
- Not "the settings are wrong": the top of the charge is right. The
  floor is a policy choice sized to a scale that turned out to be an
  estimate; it is re-sized after T2, not before.
- Not the flat-voltage argument on its own; it is the 1 A-in-while-
  falling fact that carries the idle-slide verdict.
