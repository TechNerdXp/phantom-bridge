"""Phantom II bridge -- site config. Every site-specific value lives here.

Anything you need to change for this install should be in this file. To keep
local edits out of git, put overrides in `config.local.py` (already ignored);
it is exec'd at the bottom of this file if present.
"""

# --------------------------------------------------------------------------
# The dongle
# --------------------------------------------------------------------------

# From WatchPower -> Wi-Fi Module Information.
#
# These two are PLACEHOLDERS, deliberately. The real values are site
# identifiers and live in `config.local.py`, which is gitignored, so this repo
# can be public. The PN is not a password, but on this collector family it is
# how a device is claimed in the vendor app and it doubles as the SoftAP SSID
# -- published next to that family's default AP password it becomes an
# actionable pair for anyone within WiFi range.
#
# Copy config.local.py.example (or write the two lines yourself) to set them.
DONGLE_PN = "W00344XXXXXXXX"
DONGLE_FW = "3.6.6.6"

# None = discover by broadcast. Set the real address in config.local.py:
# unicast is faster and more reliable than the sweep. It is a first guess,
# not a promise: every `set>` goes to this address *and* by broadcast, and
# the collector follows the dongle if DHCP moves it. A DHCP reservation on
# the router it hangs off is the only thing that makes this address stable.
DONGLE_IP = None

# UDP channel that accepts `set>...;` config strings.
UDP_CONFIG_PORT = 58899

# If the dongle listened on TCP itself we could connect *out* and never touch
# its config. Checked 2026-09-19: 8899, 502, 80, 23 and 18899 are all CLOSED on
# this dongle, so the redirect is the only way in and WatchPower does go quiet
# while we hold the link. Kept for the next firmware, or a different dongle.
DIRECT_TCP_PORT = 8899


# --------------------------------------------------------------------------
# This machine
# --------------------------------------------------------------------------

# None = detect the LAN address automatically, which survives DHCP changes.
# Set a string only if detection picks the wrong interface.
LOCAL_IP = None
LOCAL_PORT = 8899

# Netmask width for computing broadcast addresses. /24 is right on virtually
# every home network; change it if this site is not one.
LAN_CIDR_BITS = 24


# --------------------------------------------------------------------------
# Framing -- CONFIRMED against the unit 2026-09-19
# --------------------------------------------------------------------------

# How the Eybond header's length field is read: "tail" (payload + 2),
# "body" (payload), "frame" (payload + 8), or "raw" (no header at all).
# "tail" is confirmed: every reply came back CRC-valid under it.
EYBOND_DIALECT = "tail"

# Re-pick the dialect automatically the first time a CRC-valid PI30 reply
# arrives. This is how the guess above gets settled; leave it on.
AUTO_DIALECT = True

DEVCODE = 0x0993      # Axpert / Voltronic family
DEVADDR = 0x01        # inverter address on the RS-485 bus


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------

POLL_INTERVAL = 5           # seconds between cycles on grid
BATTERY_POLL_INTERVAL = 1   # tighter cadence once we are on battery
# While waiting for the dongle to connect after a redirect, send the redirect
# again this often. One is not always enough: a dongle still holding the
# previous collector's half-open session acknowledges the first and moves
# on the second (seen 2026-09-20).
REDIRECT_RETRY_SECONDS = 30
COMMAND_TIMEOUT = 6.0       # per-command reply timeout
COMMAND_GAP = 0.25          # pause between commands in a cycle

# Settled 2026-09-19: QPGS0-3 all NAK and QPIRI reports parallel_max_units = 1.
# This is a SINGLE chassis, so there is no per-unit command worth sending and
# this list is empty -- one less command on the RS-485 bus every cycle.
PARALLEL_COMMANDS = []

# Read once per session rather than every cycle: ratings, firmware, flags.
# All five confirmed answering. (QVFW2, QOPM, QBOOT and QPGS* NAK on this
# firmware -- do not add them.)
SESSION_COMMANDS = ["QPI", "QID", "QVFW", "QPIRI", "QFLAG"]


# --------------------------------------------------------------------------
# Clock sync -- goal #1
# --------------------------------------------------------------------------

SITE_TZ = "Asia/Karachi"

# Where the panels are, for the sunrise the day's readings are hooked to
# (src/sun.py). The sun is the one anchor no measurement can drift: dust,
# cloud and the house's own draw all move the turnaround, and none of them
# moves this. Degrees, north and east positive; a city centre is close
# enough -- a tenth of a degree of longitude is 24 seconds of sunrise.
# Overridden in config.local.py with the real position.
SITE_LATITUDE = 24.8607
SITE_LONGITUDE = 67.0011
SITE_UTC_OFFSET_HOURS = 5      # fallback if the tz database is unavailable

NTP_SERVERS = ["pool.ntp.org", "time.google.com", "time.cloudflare.com",
               "time.windows.com"]

# Answer the dongle's FC=1 heartbeat with site-local time, in case the firmware
# pushes it to the RTC. Note: across six sessions on 2026-09-19 this dongle sent
# NO heartbeats at all, so this path is untested and currently moot -- DAT does
# the job directly.
HEARTBEAT_TIME_SYNC = True
HEARTBEAT_TIME_FORMAT = "bin"   # "bin" | "bcd" | "ascii" -- all unverified

# Push the clock with DAT when the inverter drifts past this. The write is
# always followed by a QT readback, so an ACK that does not move the clock gets
# caught rather than believed. CONFIRMED WORKING 2026-09-19: the RTC was 15m 28s
# behind, DAT set it, and QT read back within 1s of internet time.
CLOCK_SYNC_ON_CONNECT = True
CLOCK_MAX_DRIFT_SECONDS = 60
CLOCK_RESYNC_HOURS = 24


# --------------------------------------------------------------------------
# Battery
# --------------------------------------------------------------------------

# The SOC percentage is only meaningful with closed-loop BMS comms. Without
# it, the number is voltage-derived and near-useless on a flat LiFePO4 curve.
# Leave False until BMS comms are confirmed.
TRUST_SOC = False
BATTERY_CAPACITY_AH = None      # e.g. 200 -- enables a runtime estimate
BATTERY_USABLE_FRACTION = 0.8

# What the pack was sold as, and what it delivers. Sold as 5 kWh; in
# practice about 2 kWh come out of it (the owner's experience, 2026-09-22,
# and roughly half the promised backup -- Oracle #29, open). The on-battery
# episodes read the inverter's voltage-derived SOC, and that scale implies
# the sold figure (4.8 kWh per 100 % over the first episodes), so it is not
# evidence either way. Until the pack is measured properly, every derived
# pack figure is CAPPED at the practical number: the data may say less,
# never more. BATTERY_SOLD_WH is only for the reports' "implied is X % of
# sold" line. BATTERY_PACK_WH (below) pins the figure and skips the cap.
BATTERY_SOLD_WH = 5000
BATTERY_PACK_WH_CAP = 2000

# Below this the grid is considered absent rather than merely sagging.
GRID_PRESENT_VOLTS = 80.0


# --------------------------------------------------------------------------
# Load side
# --------------------------------------------------------------------------

# The output is wired in 7/29 (7 strands of 0.029", ~3 mm^2 copper), which is
# good for roughly 25 A continuous in PVC. At 220 V that is ~5.5 kW -- the
# cable tops out before the 6 kVA inverter does, which is why the watch
# screen shows output current against THIS number and not just load%.
#
# The inverter reports one combined output; it cannot tell two lines apart.
# If the output is split over two cables downstream, this is the per-line
# rating and the reading is the sum -- a per-line figure needs a clamp or a
# metering breaker on each line.
LOAD_LINE_RATING_A = 25.0
LOAD_LINE_WARN_FRACTION = 0.8        # amber from here, red at the rating

# The grid input is a 7/29 too. The grid leg is derived (load + charge -
# solar - discharge), so its amps are derived as well: watts / grid volts.
GRID_LINE_RATING_A = 25.0

# What the pack allows, in amps at the 48 V bus, for the battery arrow.
# Amber from the continuous figure, red (and blinking) at the maximum. The
# same numbers apply to charging, which the inverter itself caps at
# QPIRI max_charge_a (90 A here) so it will always sit under the maximum.
BATTERY_CONTINUOUS_A = 50.0
BATTERY_MAX_A = 100.0

# Every leg's arrow takes its colour from how close it is to its limit --
# grid and home against the 7/29, battery against the pack -- and blinks
# at the limit. The home leg is the SUM of the two output lines against ONE
# line's rating, because the inverter cannot say how the load is split:
# red there means "at least one line could be at its limit", not "both are".


# --------------------------------------------------------------------------
# Tray
# --------------------------------------------------------------------------

# Balloon on grid-lost / grid-back. Off: around dawn this unit flips between
# line and battery mode every few seconds while the sun is not quite enough
# (2026-09-20, 07:18: five flips in a minute), and each flip was a balloon.
TRAY_NOTIFICATIONS = False

# The icon's colour ladder (the rule is spelled out over the palette in
# tray.py). Level wins over everything: red under the critical percentage or
# at the voltage floor, orange under the low percentage. Above that, three
# concerns add up a step each -- not full, heavy draw, house on the pack
# alone -- green, blue, orange, red. The SOC is voltage-derived, so the
# voltage floor is the backstop for the percentage, and "full" sits a little
# under 100 because the reading hovers there at float; set it to 100 to
# demand the digit. Heavy is amps out of the pack. The pack is sold as 5 kWh
# but delivers about 2 kWh in practice (under investigation, Oracle #29), so
# 15 A -- roughly 0.8 kW at the bus -- already empties it in a couple of
# hours and counts as heavy here.
#
# This block, the comment over the palette in tray.py and Reading.colour
# under it are the ONE place the concerns live. A bigger bank weighs them
# differently: heavy should then be time-to-empty at the current draw (from
# BATTERY_CAPACITY_AH), not a bigger amp figure, and the low bands should
# be hours left, not percent. Not done; sized to this pack only.
TRAY_LOW_SOC = 30
TRAY_CRITICAL_SOC = 20
TRAY_CRITICAL_V = 47.0
TRAY_FULL_SOC = 97
TRAY_HEAVY_DISCHARGE_A = 15.0


# --------------------------------------------------------------------------
# Energy counters
# --------------------------------------------------------------------------

# The inverter keeps per-day PV and load watt-hour counters (QED/QLD, confirmed
# 2026-09-20). Read today's this often; they only move by the watt-hour, so
# every cycle would be waste.
ENERGY_INTERVAL = 60

# How far back to pull daily history on each new session. This is what the
# "yesterday vs typical" solar verdict is judged against.
ENERGY_HISTORY_DAYS = 30

# Yesterday's PV below this fraction of the 7-day median is called out on the
# watch screen: dust, shade, or a string down. 0.7 is a big enough drop to be
# real rather than weather; tune it once a season of data is in.
PV_DROP_WARN_FRACTION = 0.7


# --------------------------------------------------------------------------
# Automatic output priority -- SBU by day, SUB in the evening, SBU again
# once the pack can carry the rest of the night. See src/policy.py.
# --------------------------------------------------------------------------

# The one switch. False: the collector works out what it WOULD set every
# cycle, logs the decisions and shows them on the watch screen and in the
# tray, and writes nothing. True: it also sends POP01 / POP02 and reads
# QPIRI back to prove the setting moved -- an ACK is not evidence here any
# more than it is for the clock.
#
# Gated on its own, like the clock, because output priority is the one
# setting the owner already flips twice a day with the inverter's own timer
# (menu 99). Turn that timer OFF before turning this on, or the two fight.
# The inverter's other setpoints (voltages, currents) stay behind
# ALLOW_WRITES, which stays False.
AUTO_PRIORITY = False

# The floor: at or under this SOC the house goes to the grid (SUB) whatever
# the time, and stays there until the sun is lifting the pack -- past the
# turnaround AND back over the resume level. The gap between the two is the
# hysteresis that stops it flapping around the floor at dawn.
AUTO_SOC_FLOOR = 30
AUTO_SOC_RESUME = 35

# The picture the decisions are made from: the last N finished days'
# analyses (logs/days/), yesterday first. Yesterday alone gives the
# turnaround and the dusk; the whole window gives the hourly load, the
# pack size the episodes imply and the battery-to-load efficiency.
AUTO_HISTORY_DAYS = 7

# Dusk is the end of the last hour whose mean PV was at least this fraction
# of the day's best hour, from yesterday. 0.25 puts it an hour before the
# panels go dark, which is when the pack would otherwise start carrying
# the house and spending the reserve an outage may need.
AUTO_DUSK_PV_FRACTION = 0.25

# Used only until there are days to read, or when a day has no usable
# figure. HH:MM site time; watts.
AUTO_DEFAULT_TURNAROUND = "07:00"
AUTO_DEFAULT_DUSK = "17:00"
AUTO_DEFAULT_NIGHT_LOAD_W = 400

# The pack in SOC-scale watt-hours: what 100 % of the inverter's SOC is
# worth. None = the median the on-battery episodes imply (Wh drawn per SOC
# point), which is the right scale even though the SOC is voltage-derived,
# because SOC points are what the floor and the release are measured in.
# A number pins it. Falls back to BATTERY_CAPACITY_AH x 51.2, then to
# AUTO_FALLBACK_PACK_WH. Whatever is derived is capped at
# BATTERY_PACK_WH_CAP (see the Battery block); the pin is not.
BATTERY_PACK_WH = None
AUTO_FALLBACK_PACK_WH = 2000

# The release keeps this much above the floor beyond the expected draw to
# the turnaround. Switch-X still grants the motor's two-minute burst and
# the cooker's five minutes on the pack after the release; without a
# margin those land the pack on the floor before the sun. What is left
# over is published as `headroom_wh` in the policy payload -- the Wh an
# outside caller may spend. 300 Wh is a cooker run and a couple of motor
# bursts on this pack.
AUTO_RELEASE_MARGIN_WH = 300

# The night is settled in SOC POINTS, not watt-hours (the owner's rule,
# 2026-09-23): aim to LAND on the floor exactly at the turnaround, nothing
# left over and nothing short. Points because the pack's Wh scale is the one
# figure nobody has (Oracle #29) -- pack_wh sits pinned at the 2000 Wh cap,
# 20 Wh a point, while the logs measure 48-55 Wh a point out, so the
# watt-hour chain thinks the pack is 2.5x emptier than it is and holds the
# release hours too late. Points per kWh of house load is measured straight
# off LAST NIGHT's episodes and folds the inverter's losses in with it; this
# is only the value used until a night has been logged.
AUTO_POINTS_PER_KWH = 19.0

# The evening belongs to the reserve. For this long after dusk the pack is
# never spent on the house however full it is: outages cluster in those
# hours and the heavy motors run then (owner, 2026-09-23). Without it a
# light winter night would release at dusk, straight into that window.
AUTO_EVENING_RESERVE_H = 4
#
# NOTE, owner 2026-09-23, not implemented and waiting on data: if winter
# mornings keep landing well above the floor with the trim already saturated
# against this guard, that is the data asking for the window to reach back
# towards dusk -- shrink this. It is the hard case (the motors and the
# outages are real), so it is a judgement to make once the trim log shows
# the pattern, not a rule to automate now.

# Last night's landing, carried into tonight. The miss is the SOC at the
# turnaround against the floor we aimed at: landed above it and points went
# unspent, so widen tonight's window at the FRONT (release earlier); landed
# under it and shorten at the BACK instead (stand down before dawn, keeping
# the evening reserve intact). The patch is in points and is converted to
# clock time through the load, because an hour at 600 W costs three times an
# hour at 200 W. Half the miss each night, so one odd night cannot swing the
# plan, and the whole carried figure is clamped.
AUTO_TRIM_GAIN = 0.5
AUTO_TRIM_MAX_POINTS = 20

# The pack is never held past this time of night, whatever the arithmetic
# says: at this clock the reserve is released (SBU) and the pack runs the
# house down to the floor, however long that takes. The owner's rule of
# 2026-09-22, after the first autopilot night held a 40 % pack on the
# grid until dawn (the release needs SOC ~45 % on the 2 kWh scale, and the
# idle SOC slid 40 -> 34 for nothing): the floor IS the reserve. This is
# what the panel's menu-99 timer did (SBU from 02:00) and the plan must
# never be less generous than it; the arithmetic can only release EARLIER
# than this, never later. None = arithmetic only.
AUTO_RELEASE_LATEST = "02:00"

# An outside request (Switch-X writes logs/request.json: {"want": "SUB",
# "until": "HH:MM", "why": "..."}) holds that setting ahead of the plan,
# never past the floor rule, for at most this many minutes from when the
# collector first saw it -- so a stuck file cannot keep the pack off all
# night. Removing the file ends the hold. Ignored, and said so in the
# reason, if the grid was absent when it arrived.
AUTO_REQUEST_MAX_MIN = 60

# Load watt-hours per battery watt-hour on a night episode. Derived from
# the episodes when there are enough; this is the fallback.
AUTO_EFFICIENCY_FALLBACK = 0.88

# No two writes closer than this, except the floor rule, which is
# protective and may write at once. Also the back-off after a NAK or a
# write that did not move QPIRI.
AUTO_MIN_DWELL_S = 600

# How often the collector re-reads QPIRI to confirm the priority is what it
# thinks it is. A change it did not make is logged as an external one --
# that is what the inverter's own timer looks like from here.
AUTO_VERIFY_INTERVAL_S = 300


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------

# Read-only until proven. Flip to True only when you mean it. This gates every
# setting that changes how the machine behaves -- priorities, voltages,
# currents, equalization, factory reset.
ALLOW_WRITES = False

# The clock is gated separately, and is on. The reason ALLOW_WRITES exists is
# that a bad setpoint can make paralleled units fight each other or mistreat a
# battery. The RTC can do neither: it drives the inverter's own log timestamps
# and nothing else. Keeping it under the same gate as charge voltages would
# mean the project's first goal could not be met without also unlocking the
# dangerous half of the catalogue.
ALLOW_CLOCK_WRITES = True

# Controls that must never be written to one unit alone on a parallel stack.
# ctl.py refuses these without --unit or --all-units once a twin is confirmed.
PARALLEL_CONFIRMED = False      # None = unknown, True = twin, False = single
                                # Settled 2026-09-19: single 6 kVA chassis.


# --------------------------------------------------------------------------
# Local overrides -- config.local.py is gitignored
# --------------------------------------------------------------------------

def _project_root():
    """The folder that owns config.local.py and logs/.

    From source that is this file's folder. In the frozen exe this module
    lives inside the bundle, so the root is found from the exe instead: the
    first ancestor of dist\\PhantomBridge\\PhantomBridge.exe that holds a
    config.py, which is the checkout two levels up. That rule also holds if
    the exe is ever copied next to the sources, and it fails soft to the
    exe's own folder rather than to a temp dir.
    """
    import pathlib
    import sys
    if getattr(sys, "frozen", False):
        here = pathlib.Path(sys.executable).resolve().parent
        for candidate in (here, *here.parents):
            if (candidate / "config.py").exists():
                return candidate
        return here
    return pathlib.Path(__file__).resolve().parent


ROOT = _project_root()


def _load_local_overrides() -> None:
    local = ROOT / "config.local.py"
    if local.exists():
        exec(compile(local.read_text(encoding="utf-8"), str(local), "exec"),
             globals())


_load_local_overrides()
