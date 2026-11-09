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

# ---------------- motor runs ----------------
MAX_MOTOR_SECONDS = 300          # matches MAX_RUN_SECONDS in the firmware's config.h
MOTOR_REST_ANGLE = 0             # where the servo waits between runs
MOTOR_RUN_ANGLE = 180            # where a run turns it to

# ---------------- ESP32 link ----------------
SERIAL_BAUD = 115200
ESP32_PORT = os.getenv("ESP32_PORT") or None   # None = auto-detect
POLL_SECONDS = 1                 # how often the dashboard asks Bay 1 for its motor state
RAIN_POLL_SECONDS = 5            # how often it reads the rain sensor
HARDWARE_STALE_SECONDS = 10      # a reading older than this counts as unknown
RAIN_STALE_SECONDS = 15
# How long to wait before auto-connecting again after a failed attempt, by reason.
# A port that is there but silent is retried slowly, so the dashboard doesn't keep the
# port open and get in the way of a PlatformIO upload or serial monitor.
RETRY_SECONDS = {"not_found": 3, "missing_port": 5, "busy": 10, "permission": 30,
                 "no_answer": 20, "old_firmware": 60, "lost": 3}
RETRY_SECONDS_DEFAULT = 10

# ---------------- simulator ----------------
SIM_TICK_SECONDS = 2
