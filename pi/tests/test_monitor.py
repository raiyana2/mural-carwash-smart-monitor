import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from database import MonitorDatabase
from web_app import MonitorController, app


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
    """Stands in for Esp32Link: behaves like the servo firmware (servo_set refused during a hold)."""

    port = "STUB"

    def __init__(self):
        self.angle, self.running = 0, False

    def _status(self):
        from esp32_link import MotorStatus
        return MotorStatus(running=self.running, remaining_s=5.0 if self.running else 0.0, angle=self.angle)

    def servo_set(self, angle):
        from esp32_link import Esp32Error
        if self.running:
            raise Esp32Error("timed hold running; send motor_off first")
        self.angle = angle
        return self._status()

    def motor_status(self):
        return self._status()

    def motor_on(self, seconds, angle=None):
        self.return_to, self.angle, self.running = self.angle, 180 if angle is None else angle, True
        return self._status()

    def motor_off(self):
        if self.running:
            self.angle, self.running = self.return_to, False
        return self._status()

    def close(self):
        pass


class ServoApiTests(unittest.TestCase):
    def test_servo_angle_is_reported_and_can_be_set(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = MonitorController(
                database=MonitorDatabase(Path(temp_dir) / "monitor.sqlite3"),
                run_simulator=False,
                seed_demo_data=False,
            )
            controller.database.initialize()
            # Pretend Bay 1 is connected with a fresh reading.
            controller._link = StubLink()
            controller.database.update_bay(1, {"last_seen_at": time.time(), "motor_running": 0,
                                               "motor_remaining_s": 0.0, "motor_updated_at": time.time()})
            with patch("web_app.controller", controller), patch.object(controller, "start"), \
                    patch.object(controller, "close"):
                with TestClient(app) as client:
                    moved = client.post("/api/bays/1/servo", json={"angle": 90})
                    self.assertEqual(moved.status_code, 200)
                    self.assertEqual(moved.json()["servo_angle"], 90)
                    self.assertEqual(client.get("/api/dashboard").json()["bays"][0]["servo_angle"], 90)

                    self.assertEqual(client.post("/api/bays/1/servo", json={"angle": 181}).status_code, 422)
                    self.assertEqual(client.post("/api/bays/2/servo", json={"angle": 45}).status_code, 422)

                    # During a wash the move is refused, and the link is NOT treated as lost.
                    started = client.post("/api/bays/1/start", json={"seconds": 5})
                    self.assertEqual(started.status_code, 200)
                    self.assertEqual(started.json()["servo_angle"], 180)
                    self.assertEqual(client.post("/api/bays/1/servo", json={"angle": 45}).status_code, 409)
                    self.assertIsNotNone(controller._link)

                    # Stopping returns the servo to where it was before the wash.
                    stopped = client.post("/api/bays/1/stop")
                    self.assertEqual(stopped.status_code, 200)
                    self.assertEqual(stopped.json()["servo_angle"], 90)


if __name__ == "__main__":
    unittest.main()
