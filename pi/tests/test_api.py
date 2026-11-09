import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from carwash.controllers import MonitorController
from carwash.storage.database import MonitorDatabase
from carwash.web.app import app
from tests.helpers import StubLink, api_client, connected_controller


class DashboardApiTests(unittest.TestCase):
    def test_dashboard_settings_and_scenario_routes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = MonitorController(
                database=MonitorDatabase(Path(temp_dir) / "api.sqlite3"),
                serial_port="/no-such-serial-port",
                run_simulator=False,
                seed_demo_data=False,
            )
            with patch("carwash.web.app.controller", controller):
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


class MotorRunApiTests(unittest.TestCase):
    def test_run_turns_180_and_stop_turns_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            link = StubLink()
            client = api_client(self, connected_controller(temp_dir, link))

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
            client = api_client(self, controller)

            refused = client.post("/api/bays/1/start", json={"seconds": 5})
            self.assertEqual(refused.status_code, 409)
            self.assertIn("refused", refused.json()["detail"])
            self.assertIs(controller.hardware._link, link)
            self.assertFalse(any(f["fault_type"] == "serial_connection_lost"
                                 for f in controller.database.recent_faults(10)))
            self.assertFalse(any(s["status"] == "active" for s in controller.bay_detail(1)["sessions"]))

    def test_run_starts_from_rest_so_it_is_a_full_180(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            link = StubLink(angle=90)   # e.g. moved with the command-line tool
            client = api_client(self, connected_controller(temp_dir, link))
            self.assertEqual(client.post("/api/bays/1/start", json={"seconds": 3}).status_code, 200)
            self.assertEqual(link.calls[:2], [("servo_set", 0), ("motor_on", 3, 180)])
            self.assertEqual(client.post("/api/bays/1/stop").json()["servo_angle"], 0)

    def test_stop_works_when_motor_runs_without_a_recorded_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            link = StubLink()
            link.motor_on(30)       # started before the dashboard (re)started
            controller = connected_controller(temp_dir, link)
            client = api_client(self, controller)
            controller.hardware.poll_locked()
            self.assertEqual(client.get("/api/dashboard").json()["bays"][0]["motor_running"], 1)
            stopped = client.post("/api/bays/1/stop")
            self.assertEqual(stopped.status_code, 200)
            self.assertEqual(stopped.json()["motor_running"], 0)
            self.assertEqual(stopped.json()["servo_angle"], 0)

    def test_simulated_bays_mirror_the_180_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = connected_controller(temp_dir, StubLink())
            client = api_client(self, controller)
            started = client.post("/api/bays/4/start", json={"seconds": 20})
            self.assertEqual(started.json()["servo_angle"], 180)
            self.assertEqual(started.json()["remaining_seconds"], 20)
            self.assertEqual(client.post("/api/bays/4/stop").json()["servo_angle"], 0)

    def test_servo_position_endpoint_is_gone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = api_client(self, connected_controller(temp_dir, StubLink()))
            self.assertIn(client.post("/api/bays/1/servo", json={"angle": 90}).status_code, (404, 405))

    def test_out_of_service_toggle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = api_client(self, connected_controller(temp_dir, StubLink()))
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
            client = api_client(self, controller)
            if True:
                self.assertEqual(client.get("/api/bays/99").status_code, 404)
                offline = client.post("/api/bays/1/start", json={"seconds": 5})
                self.assertEqual(offline.status_code, 503)
                self.assertIn("Maintenance", offline.json()["detail"])
                self.assertEqual(client.post("/api/bays/2/start", json={"seconds": 999}).status_code, 422)

    def test_web_layer_has_no_controller_logic(self):
        web_dir = Path(__file__).parent.parent / "carwash" / "web"
        source = (web_dir / "app.py").read_text() + (web_dir / "schemas.py").read_text()
        for name in ("import serial", "from esp32_link", "from database", "import threading", "HTTPException", ".database."):
            self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()
