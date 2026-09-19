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
# unicast is faster and more reliable than the sweep.
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

# Below this the grid is considered absent rather than merely sagging.
GRID_PRESENT_VOLTS = 80.0


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

def _load_local_overrides() -> None:
    import pathlib
    local = pathlib.Path(__file__).with_name("config.local.py")
    if local.exists():
        exec(compile(local.read_text(encoding="utf-8"), str(local), "exec"),
             globals())


_load_local_overrides()
