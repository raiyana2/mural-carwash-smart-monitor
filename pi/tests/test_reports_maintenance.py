import random
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from carwash.config import FLOW_CHECK_SECONDS, MAX_MOTOR_SECONDS
from carwash.controllers import ControllerError, MonitorController
from carwash.controllers.pricing import price_for_seconds
from carwash.storage.database import MonitorDatabase
from tests.helpers import StubLink, api_client, connected_controller


class PricingTests(unittest.TestCase):
    def test_new_bays_cost_a_dollar_a_minute_up_to_an_hour(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = MonitorDatabase(Path(temp_dir) / "p.sqlite3")
            database.initialize()
            bay = database.get_bay(3)
            self.assertEqual(bay["maximum_duration_seconds"], MAX_MOTOR_SECONDS)
            self.assertEqual(MAX_MOTOR_SECONDS, 3600)
            self.assertEqual(price_for_seconds(bay, 60), 100)
            self.assertEqual(price_for_seconds(bay, 61), 200)        # a started minute counts
            self.assertEqual(price_for_seconds(bay, 5 * 60), 500)
            self.assertEqual(price_for_seconds(bay, 3600), 6000)

    def test_old_default_prices_are_upgraded_but_custom_prices_are_kept(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "old.sqlite3"
            database = MonitorDatabase(path)
            database.initialize()
            with sqlite3.connect(path) as db:   # make it look like an older database
                db.execute("UPDATE pricing_settings SET base_price_cents = 500, included_seconds = 120, "
                           "additional_price_per_minute_cents = 100, maximum_duration_seconds = 300")
                db.execute("UPDATE pricing_settings SET base_price_cents = 650 WHERE bay_number = 4")
            database.initialize()
            self.assertEqual(database.get_bay(3)["base_price_cents"], 100)
            self.assertEqual(database.get_bay(3)["maximum_duration_seconds"], 3600)
            self.assertEqual(database.get_bay(4)["base_price_cents"], 650)

    def test_lcd_shows_the_price_paid_during_a_wash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            link = StubLink()
            controller = connected_controller(temp_dir, link)
            controller.start_session(1, 5 * 60)
            self.assertEqual(link.last_label, "Wash $5.00")


class Bay1SensorHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.link = StubLink()
        self.controller = connected_controller(self.temp_dir.name, self.link)
        self.hw = self.controller.hardware

    def poll(self):
        with self.controller._lock:
            self.hw._next_distance_at = 0
            self.hw._next_rain_at = 0
            self.hw.poll_locked()

    def test_ultrasonic_counts_each_car_once(self):
        # car arrives (2 readings to confirm), stays, one missed reading, leaves, next car
        for distance in (200, 30, 25, 28, 200, 27, 30, 200, 200, 200, 40, 35, 35):
            self.link.distance_cm = distance
            self.poll()
        self.assertEqual(len(self.controller.database.readings(1, "car")), 2)
        self.assertEqual(self.controller.dashboard()["bays"][0]["cars_today"], 2)

    def test_wash_records_its_average_water_flow(self):
        self.link.rain_wet = 80.0
        self.controller.start_session(1, 60)
        self.poll()
        self.hw._wash["started"] -= 5            # pretend the water has had time to start
        for wet in (80.0, 70.0):
            self.link.rain_wet = wet
            self.poll()
        self.link.motor_off()
        self.poll()
        flows = self.controller.database.readings(1, "flow")
        self.assertEqual(len(flows), 1)
        self.assertAlmostEqual(flows[0]["value"], 7.5)   # (8.0 + 7.0) / 2 L/min

    def test_wash_with_no_water_raises_an_alert_right_away(self):
        self.link.rain_wet = 0.0
        self.controller.start_session(1, 60)
        self.poll()
        self.hw._wash["started"] -= FLOW_CHECK_SECONDS + 1
        self.poll()
        faults = [f for f in self.controller.database.recent_faults(10) if f["active"]]
        self.assertEqual([f["fault_type"] for f in faults], ["flow_none"])
        self.assertIn("No water flow", faults[0]["explanation"])
        # Bay 1 can still be used; it's an alert, not a lock-out.
        self.link.motor_off()
        self.poll()
        self.assertNotEqual(self.controller.bay_detail(1)["status"], "faulted")
        self.assertEqual(self.controller.maintenance.water(self.controller.database.get_bay(1))["status"], "down")


class SimulatedHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.controller = MonitorController(
            database=MonitorDatabase(Path(cls.temp_dir.name) / "h.sqlite3"),
            run_simulator=False, seed_demo_data=False, auto_connect=False,
        )
        cls.controller.database.initialize()
        cls.controller.simulator._random = random.Random(2)
        cls.added = cls.controller.simulator.seed_history()
        cls.report = cls.controller.maintenance_report()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def equipment(self, bay_number, name):
        bay = next(b for b in self.report["bays"] if b["bay_number"] == bay_number)
        return next(e for e in bay["equipment"] if e["equipment"] == name)

    def test_history_is_added_once_for_simulated_bays_only(self):
        self.assertGreater(self.added, 2000)
        self.assertEqual(self.controller.simulator.seed_history(), 0)
        self.assertEqual(self.controller.database.count_sessions(1), 0)
        self.assertEqual(self.controller.database.readings(1, "car"), [])

    def test_predictions_find_the_problem_bays(self):
        self.assertEqual(self.equipment(10, "water")["status"], "down")        # failed pump
        self.assertEqual(self.equipment(5, "water")["status"], "failing")     # clogging nozzle
        wearing = self.equipment(8, "water")
        self.assertIsNotNone(wearing["days_until"])
        self.assertTrue(3 <= wearing["days_until"] <= 30, wearing)
        self.assertEqual(self.equipment(3, "water")["status"], "ok")
        self.assertEqual(self.equipment(7, "valve")["status"], "soon")        # busiest bay
        self.assertEqual(self.equipment(7, "valve")["days_until"], 0)
        self.assertEqual(self.equipment(4, "counter")["status"], "ok")
        self.assertEqual(self.equipment(1, "water")["status"], "no_data")     # real bay: no history yet

    def test_alerts_were_raised_for_the_problems(self):
        active = {(f["bay_number"], f["fault_type"]) for f in self.controller.database.recent_faults(100) if f["active"]}
        self.assertIn((10, "flow_none"), active)
        self.assertIn((5, "flow_low"), active)
        self.assertIn((7, "valve_service_due"), active)
        self.assertNotIn((3, "flow_low"), active)

    def test_financial_report_adds_up(self):
        report = self.controller.financial_report(30)
        self.assertEqual(len(report["daily"]), 30)
        self.assertEqual(len(report["by_hour"]), 24)
        self.assertEqual(sum(d["revenue_cents"] for d in report["daily"]), report["totals"]["revenue_cents"])
        self.assertEqual(sum(b["revenue_cents"] for b in report["by_bay"]), report["totals"]["revenue_cents"])
        self.assertEqual(sum(d["washes"] for d in report["daily"]), report["totals"]["washes"])
        self.assertGreater(report["totals"]["cars"], report["totals"]["washes"])   # some drive through
        self.assertEqual(report["totals"]["simulated_revenue_cents"], report["totals"]["revenue_cents"])
        busiest = max(report["by_hour"], key=lambda h: h["washes"])["hour"]
        self.assertTrue(9 <= busiest <= 19)
        # 30 days of history: a 7-day report can compare with the week before, a 30-day one can't.
        self.assertFalse(report["previous_complete"])
        week = self.controller.financial_report(7)
        self.assertTrue(week["previous_complete"])
        self.assertGreater(week["previous"]["washes"], 0)

    def test_marking_serviced_starts_afresh_and_clears_alerts(self):
        controller = self.controller
        controller.mark_serviced(10, "water")
        active = {(f["bay_number"], f["fault_type"]) for f in controller.database.recent_faults(100) if f["active"]}
        self.assertNotIn((10, "flow_none"), active)
        self.assertGreater(controller.simulator.flow_at(10, time.time()), 5)    # pump fixed
        with self.assertRaises(ControllerError):
            controller.mark_serviced(10, "wheels")


class ReportApiTests(unittest.TestCase):
    def test_report_and_maintenance_routes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = connected_controller(temp_dir, StubLink())
            controller.simulator.seed_history(days=3)
            client = api_client(self, controller)
            report = client.get("/api/reports?days=7")
            self.assertEqual(report.status_code, 200)
            self.assertEqual(len(report.json()["daily"]), 7)
            self.assertEqual(client.get("/api/reports?days=0").status_code, 422)
            maintenance = client.get("/api/maintenance").json()
            self.assertEqual(len(maintenance["bays"]), 11)
            self.assertEqual([e["equipment"] for e in maintenance["bays"][0]["equipment"]], ["water", "valve", "counter"])
            self.assertEqual(client.post("/api/maintenance/3/serviced", json={"equipment": "valve"}).status_code, 200)
            self.assertEqual(client.post("/api/maintenance/3/serviced", json={"equipment": "x"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
