import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from carwash.controllers import ControllerError, MonitorController
from carwash.hardware.esp32_link import Esp32ConnectError
from carwash.storage.database import MonitorDatabase
from carwash.config import BUTTON_RUN_SECONDS
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
        self.assertEqual(session["anomaly_estimate_cents"], 400)   # bought 5 min ($5), got 30 s ($1)
        self.assertEqual(result["faults"][0]["fault_type"], "purchased_granted_mismatch")
        self.assertEqual(self.database.dashboard()["simulated_revenue_cents"], 500)
        self.assertEqual(self.database.analytics()["totals"]["anomaly_estimate_cents"], 400)

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



class Bay1SensorTests(unittest.TestCase):
    """The ultrasonic sensor, the button and the LCD on Bay 1's ESP32."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def poll(self, controller):
        with controller._lock:
            controller.hardware._next_distance_at = 0
            controller.hardware.poll_locked()

    def test_ultrasonic_reading_says_whether_a_car_is_in_the_bay(self):
        link = StubLink()
        controller = connected_controller(self.temp_dir.name, link)
        for distance, present in ((23.5, True), (180.0, False), (None, False)):
            link.distance_cm = distance
            self.poll(controller)
            bay1 = controller.dashboard()["bays"][0]
            self.assertEqual(bay1["distance_cm"], distance)
            self.assertEqual(bay1["vehicle_present"], int(present))
            self.assertEqual(bay1["vehicle_sensor_installed"], 1)

    def test_older_firmware_without_the_new_sensors_still_works(self):
        link = StubLink(features={"motor", "rain"})
        controller = connected_controller(self.temp_dir.name, link)
        self.poll(controller)
        bay1 = controller.dashboard()["bays"][0]
        self.assertIsNone(bay1["vehicle_present"])
        self.assertEqual(bay1["vehicle_sensor_installed"], 0)
        self.assertFalse(any(call[0] in ("distance", "lcd_text") for call in link.calls))
        self.assertEqual(controller.start_session(1, 5)["servo_angle"], 180)

    def test_button_press_starts_a_run(self):
        link = StubLink()
        controller = connected_controller(self.temp_dir.name, link)
        woken = []
        controller.hardware.wake = lambda: woken.append(True)
        controller.hardware._on_esp_event({"type": "event", "event": "button", "pressed": True})
        self.assertEqual(woken, [True])
        self.poll(controller)
        self.assertIn(("motor_on", BUTTON_RUN_SECONDS, 180), link.calls)
        bay1 = controller.bay_detail(1)
        self.assertEqual(bay1["servo_angle"], 180)
        self.assertTrue(any(s["status"] == "active" for s in bay1["sessions"]))
        self.assertIn("Started", controller.dashboard()["hardware"]["last_button_result"])

        # Pressing again while it runs adds a minute instead of starting a second run.
        controller.hardware._on_esp_event({"type": "event", "event": "button", "pressed": True})
        self.poll(controller)
        self.assertEqual(sum(1 for call in link.calls if call[0] == "motor_on"), 1)
        self.assertIn(("motor_add", BUTTON_RUN_SECONDS), link.calls)
        self.assertIn("Added", controller.dashboard()["hardware"]["last_button_result"])

    def test_button_is_ignored_when_the_bay_is_out_of_service(self):
        link = StubLink()
        controller = connected_controller(self.temp_dir.name, link)
        controller.set_enabled(1, False)
        controller.hardware._on_esp_event({"type": "event", "event": "button", "pressed": True})
        self.poll(controller)
        self.assertFalse(any(call[0] == "motor_on" for call in link.calls))
        self.assertIn("out of service", controller.dashboard()["hardware"]["last_button_result"])

    def test_lcd_follows_the_bay_and_is_only_sent_when_it_changes(self):
        link = StubLink()
        controller = connected_controller(self.temp_dir.name, link)
        self.poll(controller)
        self.assertEqual(link.lcd, ["$1.00 per minute", "Max 60 min"])
        sent = sum(1 for call in link.calls if call[0] == "lcd_text")
        self.poll(controller)
        self.assertEqual(sum(1 for call in link.calls if call[0] == "lcd_text"), sent)

        controller.set_enabled(1, False)
        self.poll(controller)
        self.assertEqual(link.lcd, ["Out of service", ""])
        controller.set_enabled(1, True)
        self.poll(controller)
        self.assertEqual(link.lcd[0], "$1.00 per minute")

        controller.disconnect_hardware()
        self.assertEqual(link.lcd, ["Bay 1 offline", ""])


if __name__ == "__main__":
    unittest.main()
