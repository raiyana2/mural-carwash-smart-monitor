import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from carwash.controllers import ControllerError, MonitorController
from carwash.hardware.esp32_link import Esp32ConnectError
from carwash.storage.database import MonitorDatabase
from tests.helpers import StubLink, connected_controller


class MonitorControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "monitor.sqlite3"
        self.database = MonitorDatabase(self.db_path)
        self.database.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

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


class SimulatorScenarioTests(unittest.TestCase):
    def test_every_test_fault_on_the_maintenance_page_works(self):
        for scenario, fault_type, severity in (
            ("motor_feedback_missing", "motor_feedback_missing", "critical"),
            ("water_not_detected", "water_not_detected", "warning"),
            ("session_overrun", "session_overrun", "critical"),
            ("fault", "operator_test_fault", "warning"),
        ):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp_dir:
                controller = connected_controller(temp_dir, StubLink())
                bay = controller.trigger_scenario(3, scenario)
                self.assertEqual(bay["faults"][0]["fault_type"], fault_type)
                self.assertEqual(bay["status"] == "faulted", severity == "critical")
                # A faulted run can still be turned back, and resolving clears the fault.
                controller.stop_session(3)
                controller.resolve_fault(bay["faults"][0]["id"])
                self.assertNotEqual(controller.bay_detail(3)["status"], "faulted")

    def test_scenarios_refuse_the_hardware_bay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = connected_controller(temp_dir, StubLink())
            with self.assertRaises(ControllerError) as raised:
                controller.trigger_scenario(1, "fault")
            self.assertEqual(raised.exception.kind, "invalid")


class ConnectionTests(unittest.TestCase):
    def test_disconnect_pauses_auto_connect_until_connect(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = connected_controller(temp_dir, StubLink())
            controller.disconnect_hardware()
            self.assertIsNone(controller.hardware._link)
            with patch("carwash.controllers.hardware_bay.Esp32Link") as link_class:
                controller.hardware._next_connect_at = 0
                controller.hardware.maybe_auto_connect()
                link_class.assert_not_called()
            self.assertFalse(controller.dashboard()["hardware"]["auto_connect"])

    def test_failed_connect_explains_and_backs_off(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = connected_controller(temp_dir, StubLink())
            error = Esp32ConnectError("COM5 opened, but the ESP32 didn't answer.", "no_answer", "COM5")
            with patch("carwash.controllers.hardware_bay.Esp32Link") as link_class:
                link_class.return_value.open.side_effect = error
                with self.assertRaises(ControllerError) as raised:
                    controller.connect_hardware("COM5")
            self.assertEqual(raised.exception.kind, "unavailable")
            self.assertIn("didn't answer", str(raised.exception))
            hardware = controller.dashboard()["hardware"]
            self.assertEqual(hardware["last_error_kind"], "no_answer")
            self.assertGreaterEqual(hardware["retry_in_s"], 15)


if __name__ == "__main__":
    unittest.main()
