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


class StubLink:
    """Stands in for Esp32Link: behaves like the servo firmware (servo_set refused during a run)."""

    port = "STUB"
    connected = True

    def __init__(self, angle=0):
        self.angle, self.running, self.calls = angle, False, []
        self.refuse_motor_on = None

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

    def motor_on(self, seconds, angle=None):
        self.calls.append(("motor_on", seconds, angle))
        if self.refuse_motor_on:
            raise Esp32Error(self.refuse_motor_on)
        if self.running:
            raise Esp32Error("motor already running")
        self.return_to, self.angle, self.running = self.angle, 180 if angle is None else angle, True
        return self._status()

    def motor_off(self):
        self.calls.append(("motor_off",))
        if self.running:
            self.angle, self.running = self.return_to, False
        return self._status()

    def ping(self, timeout=None):
        return 12.0

    def read_rain(self):
        return RainReading(raw=2000, wet_percent=51.2, raining=False)

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
