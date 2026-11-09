"""Settings shared by every layer. Change numbers here, not in the code that uses them."""

import os
from pathlib import Path

# ---------------- files ----------------
PI_DIR = Path(__file__).resolve().parent.parent
# Set MURAL_MONITOR_DB to keep the database somewhere else.
DATABASE_PATH = Path(os.getenv("MURAL_MONITOR_DB") or PI_DIR / "data" / "monitor.sqlite3")

# ---------------- bays ----------------
BAY_COUNT = 11
HARDWARE_BAY = 1                 # the bay wired to the real ESP32; the rest are simulated
WASH_MODES = ("Standard", "Rinse", "Foam", "Wax")

# ---------------- washes and pricing ----------------
# A wash opens the bay's valve (the servo turns from 0° to 180°) for the time paid for.
# New bays get these prices; each bay's own prices are saved in the database.
PRICE_PER_MINUTE_CENTS = 100     # $1.00 buys one minute
MAX_WASH_MINUTES = 60            # longest wash a customer can buy
MAX_MOTOR_SECONDS = MAX_WASH_MINUTES * 60   # matches MAX_RUN_SECONDS in the firmware's config.h
MOTOR_REST_ANGLE = 0             # valve closed: the bay is idle
MOTOR_RUN_ANGLE = 180            # valve open: the bay is active

# ---------------- ESP32 link ----------------
SERIAL_BAUD = 115200
ESP32_PORT = os.getenv("ESP32_PORT") or None   # None = auto-detect
POLL_SECONDS = 1                 # how often the dashboard asks Bay 1 for its motor state
RAIN_POLL_SECONDS = 2            # how often it reads the rain sensor (the water flow meter)
HARDWARE_STALE_SECONDS = 10      # a reading older than this counts as unknown
RAIN_STALE_SECONDS = 15
# How long to wait before auto-connecting again after a failed attempt, by reason.
# A port that is there but silent is retried slowly, so the dashboard doesn't keep the
# port open and get in the way of a PlatformIO upload or serial monitor.
RETRY_SECONDS = {"not_found": 3, "missing_port": 5, "busy": 10, "permission": 30,
                 "no_answer": 20, "old_firmware": 60, "lost": 3}
RETRY_SECONDS_DEFAULT = 10

# ---------------- Bay 1 sensors and button ----------------
DISTANCE_POLL_SECONDS = 1        # how often the dashboard reads the ultrasonic sensor
DISTANCE_STALE_SECONDS = 10      # an older distance reading counts as unknown
VEHICLE_PRESENT_CM = 30          # something this close or closer is a car in the bay
CAR_CONFIRM_POLLS = 2            # during a wash, a car this many readings in a row = one car counted
END_WASH_WHEN_CAR_LEAVES = True  # end the wash once the counted car is further than VEHICLE_PRESENT_CM
CAR_LEFT_POLLS = 2               # ...for this many readings in a row (ignores one bad reading)
BUTTON_RUN_SECONDS = 10          # each press of Bay 1's button buys this many seconds

# ---------------- water flow (the rain sensor stands in for a flow meter) ----------------
FLOW_FULL_SCALE_LPM = 10.0       # rain sensor fully wet = 10 litres per minute
FLOW_NOMINAL_LPM = 7.0           # normal flow during a wash, until a bay has its own history
FLOW_NONE_LPM = 0.5              # less than this during a wash = no water: not functioning
FLOW_LOW_FRACTION = 0.7          # less than 70% of the bay's normal flow = low: may be failing
FLOW_SETTLE_SECONDS = 3          # readings this soon after a wash starts are skipped
FLOW_CHECK_SECONDS = 10          # no water this long into a wash raises the alert right away

# ---------------- maintenance predictions ----------------
VALVE_SERVICE_CYCLES = 1000      # service each valve (servo) every this many open/close cycles
SIM_HISTORY_DAYS = 30            # days of simulated history a new database starts with

# ---------------- simulator ----------------
SIM_TICK_SECONDS = 2
