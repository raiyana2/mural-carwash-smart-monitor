import tempfile
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


if __name__ == "__main__":
    unittest.main()
