import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from carwash.config import BUTTON_RUN_SECONDS
from carwash.controllers import MonitorController
from carwash.hardware import esp32_link
from carwash.storage.database import MonitorDatabase


@unittest.skipUnless(os.name == "posix", "fake_esp32.py needs a Linux/macOS pseudo-terminal")
class FakeEsp32EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent.parent / "tools" / "fake_esp32.py")],
            stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.proc.stdout.close)
        self.addCleanup(self.proc.wait, 5)
        self.addCleanup(self.proc.terminate)
        self.port = self.proc.stdout.readline().strip().rsplit(" ", 1)[-1]

    def test_link_runs_180_and_turns_back(self):
        with esp32_link.Esp32Link(self.port) as esp:
            status = esp.motor_on(1, angle=180)
            self.assertEqual((status.running, status.angle), (True, 180))
            self.assertTrue(esp.wait_until_motor_stops(timeout=4))
            self.assertEqual(esp.motor_status().angle, 0)

    def test_dashboard_connects_runs_and_disconnects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = MonitorController(
                database=MonitorDatabase(Path(temp_dir) / "e2e.sqlite3"),
                run_simulator=False, seed_demo_data=False, auto_connect=False,
            )
            controller.database.initialize()
            try:
                bay = controller.connect_hardware(self.port)
                self.assertTrue(bay["connected"])
                self.assertEqual(bay["servo_angle"], 0)
                started = controller.start_session(1, 1, None)
                self.assertEqual(started["servo_angle"], 180)
                time.sleep(1.5)
                with controller._lock:
                    controller.hardware.poll_locked()
                after = controller.bay_detail(1)
                self.assertEqual(after["servo_angle"], 0)
                self.assertFalse(any(s["status"] == "active" for s in after["sessions"]))
                controller.disconnect_hardware()
                self.assertFalse(controller.bay_detail(1)["connected"])
            finally:
                controller.close()

    def test_link_reads_the_new_sensors_and_lcd(self):
        events = []
        with esp32_link.Esp32Link(self.port, on_event=events.append) as esp:
            self.assertEqual(esp.features, {"motor", "extend", "rain", "distance", "button", "lcd"})
            self.assertEqual(esp.firmware_version, 3)
            self.assertGreater(esp.read_distance(), 0)
            self.assertFalse(esp.read_button())
            self.assertEqual(esp.lcd_text(0, "a line much longer than sixteen"), "a line much long")
            esp._request("_press_button")
            time.sleep(0.2)
            self.assertIn({"type": "event", "event": "button", "pressed": True}, events)

    def test_button_on_the_board_starts_a_run_from_the_dashboard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = MonitorController(
                database=MonitorDatabase(Path(temp_dir) / "e2e.sqlite3"),
                run_simulator=False, seed_demo_data=False, auto_connect=False,
            )
            controller.start()      # the real worker loop, which button presses wake up
            try:
                bay = controller.connect_hardware(self.port)
                self.assertEqual(bay["vehicle_sensor_installed"], 1)
                self.assertIsNotNone(bay["distance_cm"])
                controller.hardware._link._request("_press_button")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and controller.bay_detail(1)["servo_angle"] != 180:
                    time.sleep(0.05)
                bay = controller.bay_detail(1)
                self.assertEqual(bay["servo_angle"], 180)
                self.assertEqual(bay["run_seconds"], BUTTON_RUN_SECONDS)
            finally:
                controller.close()


if __name__ == "__main__":
    unittest.main()
