import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import serial
from fastapi.testclient import TestClient

import esp32_link
from database import MonitorDatabase
from esp32_link import Esp32ConnectError
from controller import ControllerError, MonitorController
from web_app import app


class MonitorDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "monitor.sqlite3"
        self.database = MonitorDatabase(self.db_path)
        self.database.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_initialization_creates_eleven_bays_and_is_persistent(self):
        bays = self.database.list_bays()
        self.assertEqual(len(bays), 11)
        self.assertEqual(bays[0]["source"], "hardware")
        self.assertTrue(all(bay["source"] == "simulator" for bay in bays[1:]))

        self.database.update_settings(
            2,
            {
                "label": "North Bay",
                "enabled": 1,
                "default_wash_mode": "Foam",
                "base_price_cents": 725,
                "included_seconds": 90,
                "additional_price_per_minute_cents": 125,
                "maximum_duration_seconds": 240,
            },
        )
        reopened = MonitorDatabase(self.db_path)
        self.assertEqual(reopened.get_bay(2)["label"], "North Bay")
        self.assertEqual(reopened.get_bay(2)["base_price_cents"], 725)

    def test_simulator_sessions_faults_and_pricing_estimate_are_saved(self):
        controller = MonitorController(
            database=self.database,
            run_simulator=False,
            seed_demo_data=False,
        )
        result = controller.trigger_scenario(2, "pricing_anomaly")

        self.assertEqual(result["source"], "simulator")
        session = result["sessions"][0]
        self.assertNotEqual(session["purchased_seconds"], session["granted_seconds"])
        self.assertEqual(session["anomaly_estimate_cents"], 300)
        self.assertEqual(result["faults"][0]["fault_type"], "purchased_granted_mismatch")
        self.assertEqual(self.database.dashboard()["simulated_revenue_cents"], 800)
        self.assertEqual(self.database.analytics()["totals"]["anomaly_estimate_cents"], 300)

    def test_hardware_sensor_data_is_unknown_until_a_fresh_real_reading_exists(self):
        controller = MonitorController(
            database=self.database,
            run_simulator=False,
            seed_demo_data=False,
        )
        bay = controller.bay_detail(1)

        self.assertFalse(bay["connected"])
        self.assertIsNone(bay["rain_detected"])
        self.assertIsNone(bay["rain_wet_percent"])
        self.assertIsNone(bay["vehicle_present"])
        self.assertIsNone(bay["water_detected"])


class DashboardApiTests(unittest.TestCase):
    def test_dashboard_settings_and_scenario_routes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = MonitorController(
                database=MonitorDatabase(Path(temp_dir) / "api.sqlite3"),
                serial_port="/no-such-serial-port",
                run_simulator=False,
                seed_demo_data=False,
            )
            with patch("web_app.controller", controller):
                with TestClient(app) as client:
                    overview = client.get("/api/dashboard")
                    self.assertEqual(overview.status_code, 200)
                    self.assertEqual(len(overview.json()["bays"]), 11)
                    self.assertEqual(overview.json()["bays"][0]["rain_detected"], None)

                    saved = client.put(
                        "/api/bays/2/settings",
                        json={
                            "label": "Demo Bay Two",
                            "enabled": True,
                            "default_wash_mode": "Foam",
                            "base_price_cents": 650,
                            "included_seconds": 120,
                            "additional_price_per_minute_cents": 125,
                            "maximum_duration_seconds": 300,
                        },
                    )
                    self.assertEqual(saved.status_code, 200)
                    self.assertEqual(saved.json()["label"], "Demo Bay Two")

                    preview = client.post(
                        "/api/pricing/preview",
                        json={"bay_number": 2, "seconds": 180},
                    )
                    self.assertEqual(preview.json()["price_cents"], 775)

                    anomaly = client.post(
                        "/api/simulator/scenario",
                        json={"bay_number": 2, "scenario": "pricing_anomaly"},
                    )
                    self.assertEqual(anomaly.status_code, 200)
                    self.assertEqual(anomaly.json()["sessions"][0]["purchased_seconds"], 300)

                    hardware_scenario = client.post(
                        "/api/simulator/scenario",
                        json={"bay_number": 1, "scenario": "fault"},
                    )
                    self.assertEqual(hardware_scenario.status_code, 422)


class StubLink:
    """Stands in for Esp32Link: behaves like the servo firmware (servo_set refused during a run)."""

    port = "STUB"
    connected = True

    def __init__(self, angle=0):
        self.angle, self.running, self.calls = angle, False, []
        self.refuse_motor_on = None

    def _status(self):
        from esp32_link import MotorStatus
        return MotorStatus(running=self.running, remaining_s=5.0 if self.running else 0.0, angle=self.angle)

    def servo_set(self, angle):
        from esp32_link import Esp32Error
        self.calls.append(("servo_set", angle))
        if self.running:
            raise Esp32Error("timed hold running; send motor_off first")
        self.angle = angle
        return self._status()

    def motor_status(self):
        return self._status()

    def motor_on(self, seconds, angle=None):
        from esp32_link import Esp32Error
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
        from esp32_link import RainReading
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
    controller._link = link
    controller._ever_connected = True
    controller.database.update_bay(1, {"last_seen_at": time.time(), "motor_running": 0,
                                       "motor_remaining_s": 0.0, "motor_updated_at": time.time()})
    return controller


class MotorRunApiTests(unittest.TestCase):
    def client_for(self, controller):
        stack = ExitStack()
        stack.enter_context(patch("web_app.controller", controller))
        stack.enter_context(patch.object(controller, "start"))
        stack.enter_context(patch.object(controller, "close"))
        client = stack.enter_context(TestClient(app))
        self.addCleanup(stack.close)
        return client

    def test_run_turns_180_and_stop_turns_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            link = StubLink()
            client = self.client_for(connected_controller(temp_dir, link))

            started = client.post("/api/bays/1/start", json={"seconds": 5})
            self.assertEqual(started.status_code, 200)
            self.assertEqual(started.json()["servo_angle"], 180)
            self.assertEqual(link.calls[-1], ("motor_on", 5, 180))
            bay1 = client.get("/api/dashboard").json()["bays"][0]
            self.assertEqual(bay1["servo_angle"], 180)
            self.assertEqual(bay1["run_seconds"], 5)
            self.assertIsNotNone(bay1["remaining_seconds"])

            # A second Run while it's running is refused, and the link stays up.
            self.assertEqual(client.post("/api/bays/1/start", json={"seconds": 5}).status_code, 409)

            stopped = client.post("/api/bays/1/stop")
            self.assertEqual(stopped.status_code, 200)
            self.assertEqual(stopped.json()["servo_angle"], 0)
            self.assertFalse(any(s["status"] == "active" for s in stopped.json()["sessions"]))

    def test_esp32_refusal_does_not_drop_the_connection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            link = StubLink()
            link.refuse_motor_on = "seconds must be > 0 and <= 300"
            controller = connected_controller(temp_dir, link)
            client = self.client_for(controller)

            refused = client.post("/api/bays/1/start", json={"seconds": 5})
            self.assertEqual(refused.status_code, 409)
            self.assertIn("refused", refused.json()["detail"])
            self.assertIs(controller._link, link)
            self.assertFalse(any(f["fault_type"] == "serial_connection_lost"
                                 for f in controller.database.recent_faults(10)))
            self.assertFalse(any(s["status"] == "active" for s in controller.bay_detail(1)["sessions"]))

    def test_run_starts_from_rest_so_it_is_a_full_180(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            link = StubLink(angle=90)   # e.g. moved with the command-line tool
            client = self.client_for(connected_controller(temp_dir, link))
            self.assertEqual(client.post("/api/bays/1/start", json={"seconds": 3}).status_code, 200)
            self.assertEqual(link.calls[:2], [("servo_set", 0), ("motor_on", 3, 180)])
            self.assertEqual(client.post("/api/bays/1/stop").json()["servo_angle"], 0)

    def test_stop_works_when_motor_runs_without_a_recorded_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            link = StubLink()
            link.motor_on(30)       # started before the dashboard (re)started
            controller = connected_controller(temp_dir, link)
            client = self.client_for(controller)
            controller._poll_hardware_locked()
            self.assertEqual(client.get("/api/dashboard").json()["bays"][0]["motor_running"], 1)
            stopped = client.post("/api/bays/1/stop")
            self.assertEqual(stopped.status_code, 200)
            self.assertEqual(stopped.json()["motor_running"], 0)
            self.assertEqual(stopped.json()["servo_angle"], 0)

    def test_simulated_bays_mirror_the_180_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = connected_controller(temp_dir, StubLink())
            client = self.client_for(controller)
            started = client.post("/api/bays/4/start", json={"seconds": 20})
            self.assertEqual(started.json()["servo_angle"], 180)
            self.assertEqual(started.json()["remaining_seconds"], 20)
            self.assertEqual(client.post("/api/bays/4/stop").json()["servo_angle"], 0)

    def test_servo_position_endpoint_is_gone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = self.client_for(connected_controller(temp_dir, StubLink()))
            self.assertIn(client.post("/api/bays/1/servo", json={"angle": 90}).status_code, (404, 405))

    def test_out_of_service_toggle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = self.client_for(connected_controller(temp_dir, StubLink()))
            self.assertEqual(client.put("/api/bays/5/enabled", json={"enabled": False}).json()["enabled"], 0)
            refused = client.post("/api/bays/5/start", json={"seconds": 5})
            self.assertEqual(refused.status_code, 409)
            self.assertIn("out of service", refused.json()["detail"])
            self.assertEqual(client.put("/api/bays/5/enabled", json={"enabled": True}).json()["enabled"], 1)


class ControllerErrorApiTests(unittest.TestCase):
    def test_controller_errors_become_http_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = connected_controller(temp_dir, StubLink())
            controller.disconnect_hardware()
            with patch("web_app.controller", controller), patch.object(controller, "start"), \
                    patch.object(controller, "close"), TestClient(app) as client:
                self.assertEqual(client.get("/api/bays/99").status_code, 404)
                offline = client.post("/api/bays/1/start", json={"seconds": 5})
                self.assertEqual(offline.status_code, 503)
                self.assertIn("Maintenance", offline.json()["detail"])
                self.assertEqual(client.post("/api/bays/2/start", json={"seconds": 999}).status_code, 422)

    def test_web_app_has_no_controller_logic(self):
        import web_app
        source = Path(web_app.__file__).read_text()
        for name in ("import serial", "from esp32_link", "from database", "import threading", "HTTPException", ".database."):
            self.assertNotIn(name, source)


class ConnectionTests(unittest.TestCase):
    def test_disconnect_pauses_auto_connect_until_connect(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = connected_controller(temp_dir, StubLink())
            controller.disconnect_hardware()
            self.assertIsNone(controller._link)
            with patch("controller.Esp32Link") as link_class:
                controller._next_connect_at = 0
                controller._maybe_auto_connect()
                link_class.assert_not_called()
            self.assertFalse(controller.dashboard()["hardware"]["auto_connect"])

    def test_failed_connect_explains_and_backs_off(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = connected_controller(temp_dir, StubLink())
            error = Esp32ConnectError("COM5 opened, but the ESP32 didn't answer.", "no_answer", "COM5")
            with patch("controller.Esp32Link") as link_class:
                link_class.return_value.open.side_effect = error
                with self.assertRaises(ControllerError) as raised:
                    controller.connect_hardware("COM5")
            self.assertEqual(raised.exception.kind, "unavailable")
            self.assertIn("didn't answer", str(raised.exception))
            hardware = controller.dashboard()["hardware"]
            self.assertEqual(hardware["last_error_kind"], "no_answer")
            self.assertGreaterEqual(hardware["retry_in_s"], 15)

    def test_ports_endpoint(self):
        fake = SimpleNamespace(device="COM7", description="USB-Enhanced-SERIAL CH9102 (COM7)", vid=0x1A86, pid=0x55D4)
        other = SimpleNamespace(device="COM1", description="Communications Port (COM1)", vid=None, pid=None)
        with patch("esp32_link.list_ports.comports", return_value=[other, fake]):
            ports = esp32_link.list_serial_ports()
            self.assertEqual([p["device"] for p in ports], ["COM7", "COM1"])
            self.assertTrue(ports[0]["likely_esp32"])
            self.assertEqual(esp32_link.find_esp32_port(), "COM7")
        with patch("esp32_link.list_ports.comports", return_value=[other]):
            with self.assertRaises(Esp32ConnectError) as raised:
                esp32_link.find_esp32_port()
            self.assertEqual(raised.exception.kind, "not_found")
            self.assertIn("COM1", str(raised.exception))

    def test_open_errors_are_explained(self):
        explain = esp32_link._explain_open_error
        with patch("esp32_link.list_ports.comports", return_value=[]):
            busy = explain("COM5", serial.SerialException("could not open port 'COM5': PermissionError(13, 'Access is denied.', None, 5)"))
            self.assertEqual(busy.kind, "busy")
            self.assertIn("serial monitor", str(busy))
            missing = explain("COM9", serial.SerialException("could not open port 'COM9': FileNotFoundError(2, 'The system cannot find the file specified.', None, 2)"))
            self.assertEqual(missing.kind, "missing_port")
            locked = explain("/dev/ttyUSB0", serial.SerialException("Could not exclusively lock port /dev/ttyUSB0: [Errno 11] Resource temporarily unavailable"))
            self.assertEqual(locked.kind, "busy")


class SerialReaderTests(unittest.TestCase):
    def test_reply_split_across_reads_is_not_lost(self):
        class ChunkedSerial:
            is_open = True

            def __init__(self, chunks):
                self.chunks = list(chunks)

            @property
            def in_waiting(self):
                return len(self.chunks[0]) if self.chunks else 0

            def read(self, n):
                if not self.chunks:
                    raise serial.SerialException("done")
                return self.chunks.pop(0)

        link = esp32_link.Esp32Link("TEST")
        slot = {"done": threading.Event(), "reply": None}
        link._pending[7] = slot
        link._ser = ChunkedSerial([b'boot noise\r\n{"type":"rep', b"", b'ly","id":7,"ok":true,', b'"data":{"uptime_s":3}}\n'])
        link._read_loop()
        self.assertTrue(slot["done"].is_set())
        self.assertEqual(slot["reply"]["data"]["uptime_s"], 3)


class OldFirmwareTests(unittest.TestCase):
    def test_old_firmware_without_motor_commands_is_explained(self):
        link = esp32_link.Esp32Link("COM5")
        with patch.object(link, "_open_port"), patch("esp32_link._is_native_usb", return_value=False), \
                patch.object(link, "_read_loop"), patch.object(link, "_wait_for_answer", return_value=True), \
                patch.object(link, "motor_status", side_effect=esp32_link.Esp32Error("unknown cmd 'motor_status'")):
            with self.assertRaises(Esp32ConnectError) as raised:
                link.open()
        self.assertEqual(raised.exception.kind, "old_firmware")
        self.assertIn("src/main.cpp", str(raised.exception))


@unittest.skipUnless(os.name == "posix", "fake_esp32.py needs a Linux/macOS pseudo-terminal")
class FakeEsp32EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent.parent / "fake_esp32.py")],
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
                    controller._poll_hardware_locked()
                after = controller.bay_detail(1)
                self.assertEqual(after["servo_angle"], 0)
                self.assertFalse(any(s["status"] == "active" for s in after["sessions"]))
                controller.disconnect_hardware()
                self.assertFalse(controller.bay_detail(1)["connected"])
            finally:
                controller.close()


if __name__ == "__main__":
    unittest.main()
