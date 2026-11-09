"""Shared test helpers: a stand-in ESP32 link and a controller that thinks Bay 1 is connected."""

import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from carwash.controllers import MonitorController
from carwash.hardware.esp32_link import Esp32Error, MotorStatus, RainReading
from carwash.storage.database import MonitorDatabase
from carwash.web.app import app


ALL_FEATURES = frozenset({"motor", "extend", "rain", "distance", "button", "lcd"})


class StubLink:
    """Stands in for Esp32Link: behaves like the firmware (servo_set refused during a run).
    `features` defaults to everything the current firmware has; pass {"motor", "rain"}
    to act like the older firmware without the ultrasonic sensor, button and LCD."""

    port = "STUB"
    connected = True

    def __init__(self, angle=0, features=ALL_FEATURES):
        self.angle, self.running, self.calls = angle, False, []
        self.refuse_motor_on = None
        self.features = frozenset(features)
        self.firmware_version = 2 if "lcd" in self.features else None
        self.distance_cm = 120.0                 # what read_distance() returns; None = no echo
        self.rain_wet = 51.2                     # rain sensor wetness %, read as water flow
        self.lcd = ["", ""]

    def has(self, feature):
        return feature in self.features

    def _status(self):
        return MotorStatus(running=self.running, remaining_s=5.0 if self.running else 0.0, angle=self.angle)

    def servo_set(self, angle):
        self.calls.append(("servo_set", angle))
        if self.running:
            raise Esp32Error("timed hold running; send motor_off first")
        self.angle = angle
        return self._status()

    def motor_status(self):
        return self._status()

    def motor_on(self, seconds, angle=None, label=None):
        self.calls.append(("motor_on", seconds, angle))
        self.last_label = label
        if self.refuse_motor_on:
            raise Esp32Error(self.refuse_motor_on)
        if self.running:
            raise Esp32Error("motor already running")
        self.return_to, self.angle, self.running = self.angle, 180 if angle is None else angle, True
        return self._status()

    def motor_add(self, seconds, label=None):
        self.calls.append(("motor_add", seconds))
        if not self.running:
            raise Esp32Error("no run in progress")
        self.last_label = label
        return self._status()

    def motor_off(self):
        self.calls.append(("motor_off",))
        if self.running:
            self.angle, self.running = self.return_to, False
        return self._status()

    def ping(self, timeout=None):
        return 12.0

    def read_rain(self):
        return RainReading(raw=int(4095 - self.rain_wet * 40.95), wet_percent=self.rain_wet,
                           raining=self.rain_wet > 50)

    def read_distance(self):
        if not self.has("distance"):
            raise Esp32Error("unknown cmd 'distance'")
        self.calls.append(("distance",))
        return self.distance_cm

    def lcd_text(self, row, text):
        if not self.has("lcd"):
            raise Esp32Error("unknown cmd 'lcd_text'")
        self.calls.append(("lcd_text", row, text))
        self.lcd[row] = text[:16]
        return self.lcd[row]

    def close(self):
        pass


def connected_controller(temp_dir, link):
    controller = MonitorController(
        database=MonitorDatabase(Path(temp_dir) / "monitor.sqlite3"),
        run_simulator=False,
        seed_demo_data=False,
    )
    controller.database.initialize()
    # Pretend Bay 1 is connected with a fresh reading.
    controller.hardware._install_link_locked(link)
    controller.database.update_bay(1, {"last_seen_at": time.time(), "motor_running": 0,
                                       "motor_remaining_s": 0.0, "motor_updated_at": time.time()})
    return controller


def api_client(test, controller):
    """A TestClient for the web app wired to `controller`, closed when the test ends."""
    stack = ExitStack()
    stack.enter_context(patch("carwash.web.app.controller", controller))
    stack.enter_context(patch.object(controller, "start"))
    stack.enter_context(patch.object(controller, "close"))
    client = stack.enter_context(TestClient(app))
    test.addCleanup(stack.close)
    return client
