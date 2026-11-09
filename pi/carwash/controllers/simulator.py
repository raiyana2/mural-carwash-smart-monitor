"""simulator.py: Bays 2-11, which pretend to be bays like Bay 1.

The simulator mirrors Bay 1's wash (valve opens to 180°, stays open, closes at 0°) in the
database, starts occasional washes on its own, raises occasional faults, and injects
labelled test faults from the Maintenance page. It never touches the serial port.

It also makes up the sensor data Bay 1 gets from real hardware: one car counted per
wash (plus the odd car that drives through without washing), and the average water
flow of each wash. Each bay's flow follows a health profile (FLOW_PROFILES) so the
repair predictions have something to find: a clogging nozzle, a wearing pump, a
failed pump. A new database gets SIM_HISTORY_DAYS of this as history.

It starts and stops runs through the callables MonitorController gives it, so its
runs go through the same checks (in service, no open fault, time limit) as yours.
"""

from __future__ import annotations

import math
import random
import time
from typing import TYPE_CHECKING, Callable, Optional

from carwash.config import MOTOR_REST_ANGLE, MOTOR_RUN_ANGLE, SIM_HISTORY_DAYS, WASH_MODES
from carwash.controllers.errors import ControllerError
from carwash.controllers.pricing import price_for_seconds
from carwash.storage.database import MonitorDatabase

if TYPE_CHECKING:
    from carwash.controllers.maintenance import MaintenanceAnalyzer

DAY = 86400
WASH_MINUTES = (3, 4, 5, 5, 6, 6, 8, 10, 12, 15)   # how long simulated customers buy
# Washes a day for each simulated bay (busier bays wear their valves out sooner).
DAILY_WASHES = {2: 14, 3: 9, 4: 12, 5: 16, 6: 20, 7: 34, 8: 11, 9: 15, 10: 13, 11: 10}
# How busy each hour of the day is, relative to the others.
HOUR_WEIGHTS = [0.1, 0.05, 0.05, 0.05, 0.1, 0.3, 0.6, 1.0, 1.2, 1.3, 1.5, 1.7,
                1.8, 1.7, 1.6, 1.7, 1.9, 2.0, 1.8, 1.4, 1.0, 0.7, 0.4, 0.2]
# Water flow health, 1.0 = the bay's normal flow: (health now, change per day, pump failed).
# Bays not listed stay healthy. Marking a bay's water serviced on the Maintenance page
# puts it back to 1.0 (and fixes a failed pump).
FLOW_PROFILES = {
    5: (0.62, -0.013, False),    # nozzle clogging: already below 70% of normal
    8: (0.80, -0.008, False),    # pump wearing: drops below 70% in about two weeks
    10: (1.00, 0.0, True),       # pump failed: its last few washes had no water
}
FAILED_WASHES = 3                # a failed pump shows in this many of the bay's latest washes


class Simulator:
    def __init__(
        self,
        database: MonitorDatabase,
        start_run: Callable[[int, int, Optional[str]], dict],
        stop_run: Callable[[int], dict],
        maintenance: Optional["MaintenanceAnalyzer"] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.database = database
        self._start_run = start_run    # MonitorController.start_session
        self._stop_run = stop_run      # MonitorController.stop_session
        self.maintenance = maintenance
        self._random = rng or random.Random()
        self._anchor: Optional[float] = None
        self._failed_at: dict[int, float] = {}

    # ------------------------------------------------------------------
    # runs (called by MonitorController after its checks)
    # ------------------------------------------------------------------
    def start_run(self, bay: dict, seconds: int, wash_mode: str) -> int:
        try:
            session_id = self.database.start_session(
                bay["bay_number"], "simulator", wash_mode, seconds, seconds, price_for_seconds(bay, seconds)
            )
        except ValueError as exc:
            raise ControllerError(str(exc), "conflict") from exc
        self.database.update_bay(
            bay["bay_number"],
            {"motor_running": 1, "motor_remaining_s": float(seconds), "motor_updated_at": time.time()},
        )
        self.database.add_reading(bay["bay_number"], "car", 1, "simulator")   # the car that pulled in
        return session_id

    def stop_run(self, bay_number: int) -> None:
        had_run = self.database.end_session(bay_number, "completed") is not None
        self.database.update_bay(bay_number, {"motor_running": 0, "motor_remaining_s": 0})
        if had_run:
            self._finish_wash(bay_number)

    # ------------------------------------------------------------------
    # simulated sensors
    # ------------------------------------------------------------------
    def _anchor_time(self) -> float:
        """The moment the flow profiles' "health now" refers to; saved so it survives restarts."""
        if self._anchor is None:
            saved = self.database.get_meta("sim_anchor")
            if saved is None:
                saved = str(time.time())
                self.database.set_meta("sim_anchor", saved)
            self._anchor = float(saved)
        return self._anchor

    def _failed_time(self, bay_number: int) -> float:
        """When a failed-pump bay's pump stopped (saved, so it survives restarts)."""
        if bay_number not in self._failed_at:
            saved = self.database.get_meta(f"sim_pump_failed_{bay_number}")
            self._failed_at[bay_number] = float(saved) if saved else self._anchor_time() - 3 * 3600
        return self._failed_at[bay_number]

    def flow_at(self, bay_number: int, when: float, serviced: Optional[float] = -1.0) -> float:
        """Simulated average water flow (L/min) of a wash at time `when`. `serviced` is when
        the bay's water equipment was last serviced (looked up if not given)."""
        normal = 7.4 + (bay_number * 0.37) % 1.2
        health_now, per_day, failed = FLOW_PROFILES.get(bay_number, (1.0, 0.0, False))
        anchor = self._anchor_time()
        if serviced == -1.0:
            serviced = self.database.serviced_at(bay_number, "water")
        if serviced and when >= serviced:
            health = 1.0 + per_day * (when - serviced) / DAY      # fixed: healthy again, then wears as before
        else:
            health = health_now + per_day * (when - anchor) / DAY
            if failed and when >= self._failed_time(bay_number):
                health = 0.0
        health = max(0.0, min(1.05, health))
        return round(max(0.0, normal * health * self._random.gauss(1.0, 0.04)), 2)

    def _finish_wash(self, bay_number: int) -> None:
        """A simulated wash just ended: record its water flow and check the equipment."""
        bay = self.database.get_bay(bay_number)
        if not bay or not bay["water_sensor_installed"]:
            return
        self.database.add_reading(bay_number, "flow", self.flow_at(bay_number, time.time()), "simulator")
        if self.maintenance:
            self.maintenance.evaluate_after_run(bay_number, "simulator")

    def seed_history(self, days: int = SIM_HISTORY_DAYS, now: Optional[float] = None) -> int:
        """Fill a new database with `days` of simulated washes, cars and water flow for
        Bays 2-11, so the reports and predictions have something to show. Runs once per
        database. Returns the number of washes added."""
        if self.database.get_meta("sim_history_seeded"):
            return 0
        now = now or time.time()
        self.database.set_meta("sim_anchor", str(now))
        self._anchor = now
        sessions, readings = [], []
        for bay in self.database.list_bays():
            if bay["source"] != "simulator":
                continue
            bay_number = bay["bay_number"]
            per_day = DAILY_WASHES.get(bay_number, 12)
            serviced = self.database.serviced_at(bay_number, "water")
            runs = []
            for day in range(days, -1, -1):
                day_start = time.mktime(time.localtime(now - day * DAY)[:3] + (0, 0, 0, 0, 0, -1))
                weekend = time.localtime(day_start).tm_wday >= 5
                for hour, weight in enumerate(HOUR_WEIGHTS):
                    expected = per_day * (1.3 if weekend else 1.0) * weight / sum(HOUR_WEIGHTS)
                    for _ in range(self._poisson(expected)):
                        start = day_start + hour * 3600 + self._random.uniform(0, 3600)
                        seconds = self._random.choice(WASH_MINUTES) * 60
                        end = start + seconds
                        if end > now - 60:
                            continue
                        sessions.append((bay_number, "simulator", bay["default_wash_mode"], seconds,
                                         price_for_seconds(bay, seconds), start, end))
                        readings.append((bay_number, "simulator", "car", 1, start))
                        runs.append(end)
                    # The odd car that drives through without buying a wash.
                    for _ in range(self._poisson(expected * 0.08)):
                        when = day_start + hour * 3600 + self._random.uniform(0, 3600)
                        if when < now:
                            readings.append((bay_number, "simulator", "car", 1, when))
            runs.sort()
            if FLOW_PROFILES.get(bay_number, (1, 0, False))[2] and len(runs) >= FAILED_WASHES:
                failed_at = runs[-FAILED_WASHES] - 1
                self._failed_at[bay_number] = failed_at
                self.database.set_meta(f"sim_pump_failed_{bay_number}", str(failed_at))
            readings.extend((bay_number, "simulator", "flow", self.flow_at(bay_number, end, serviced), end)
                            for end in runs)
        self.database.insert_history_sessions(sessions)
        self.database.add_readings(readings)
        self.database.set_meta("sim_history_seeded", str(now))
        if self.maintenance:
            for bay_number in DAILY_WASHES:
                if self.database.get_bay(bay_number):
                    self.maintenance.evaluate_after_run(bay_number, "simulator")
        return len(sessions)

    def _poisson(self, mean: float) -> int:
        """A random count with the given average (Knuth's method; fine for small means)."""
        limit, k, p = math.exp(-mean), 0, 1.0
        while True:
            p *= self._random.random()
            if p <= limit:
                return k
            k += 1

    @staticmethod
    def decorate(bay: dict, now: float) -> dict:
        bay["connected"] = True
        bay["connected_port"] = None
        bay["features"] = []
        bay["distance_cm"] = None
        bay["servo_angle"] = MOTOR_RUN_ANGLE if bay["motor_running"] else MOTOR_REST_ANGLE
        if bay.get("active_started_at") is not None:
            elapsed = max(0, int(now - bay["active_started_at"]))
            bay["remaining_seconds"] = max(0, bay["active_granted_seconds"] - elapsed)
        return bay

    # ------------------------------------------------------------------
    # demo data
    # ------------------------------------------------------------------
    def seed_demo_data(self) -> None:
        """Give a new database some history: three finished runs and one in progress."""
        with self.database.connection() as db:
            if db.execute("SELECT COUNT(*) FROM sessions WHERE source = 'simulator'").fetchone()[0]:
                return
        now = time.time()
        for bay_number, age in ((3, 3600), (4, 2400), (6, 1200)):
            bay = self.database.get_bay(bay_number)
            session_id = self.database.start_session(
                bay_number, "simulator", "Standard", 120, 120, price_for_seconds(bay, 120)
            )
            with self.database.connection() as db:
                db.execute(
                    "UPDATE sessions SET status = 'completed', started_at = ?, ended_at = ? WHERE id = ?",
                    (now - age, now - age + 120, session_id),
                )
                db.execute(
                    """
                    UPDATE bays SET status = 'available', vehicle_present = 0,
                        water_detected = 0, motor_running = 0 WHERE bay_number = ?
                    """,
                    (bay_number,),
                )
        bay = self.database.get_bay(2)
        session_id = self.database.start_session(2, "simulator", "Foam", 180, 180, price_for_seconds(bay, 180))
        with self.database.connection() as db:
            db.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (now - 45, session_id))
            db.execute(
                "UPDATE bays SET motor_running = 1, motor_remaining_s = 135, motor_updated_at = ? WHERE bay_number = 2",
                (now,),
            )

    # ------------------------------------------------------------------
    # test faults (Maintenance page)
    # ------------------------------------------------------------------
    def trigger(self, bay_number: int, scenario: str) -> None:
        bay = self.database.get_bay(bay_number)
        if bay is None or bay["source"] != "simulator":
            raise ControllerError("Scenarios can only target simulated Bays 2–11", "invalid")
        handler = {
            "arrival": self._arrival,
            "departure": self._departure,
            "mode_change": self._mode_change,
            "pricing_anomaly": self._pricing_anomaly,
            "motor_feedback_missing": lambda bay: self._sensor_fault(bay, "motor_feedback_missing"),
            "water_not_detected": lambda bay: self._sensor_fault(bay, "water_not_detected"),
            "session_overrun": self._session_overrun,
            "fault": self._operator_fault,
        }.get(scenario)
        if handler is None:
            raise ControllerError("Unknown simulator scenario", "invalid")
        handler(bay)

    def _arrival(self, bay: dict) -> None:
        if bay["status"] == "occupied":
            raise ControllerError("Bay already has an active session", "conflict")
        self._start_run(bay["bay_number"], min(180, bay["maximum_duration_seconds"]), bay["default_wash_mode"])

    def _departure(self, bay: dict) -> None:
        if bay["status"] != "occupied":
            raise ControllerError("Bay has no active session", "conflict")
        self._stop_run(bay["bay_number"])

    def _mode_change(self, bay: dict) -> None:
        mode = next(mode for mode in WASH_MODES if mode != bay["wash_mode"])
        self.database.update_bay(bay["bay_number"], {"wash_mode": mode, "default_wash_mode": mode})
        with self.database.connection() as db:
            db.execute(
                "UPDATE sessions SET wash_mode = ? WHERE bay_number = ? AND status = 'active'",
                (mode, bay["bay_number"]),
            )

    def _pricing_anomaly(self, bay: dict) -> None:
        if bay["status"] == "occupied":
            raise ControllerError("End the current session before injecting an anomaly", "conflict")
        if bay["status"] == "faulted":
            raise ControllerError("Resolve the active bay fault before injecting a pricing anomaly", "conflict")
        if bay["maximum_duration_seconds"] < 31:
            raise ControllerError(
                "Increase this simulator bay's maximum to at least 31 seconds to demonstrate a time mismatch",
                "conflict",
            )
        bay_number = bay["bay_number"]
        purchased, granted = min(300, bay["maximum_duration_seconds"]), 30
        amount = price_for_seconds(bay, purchased)
        estimated = max(0, amount - price_for_seconds(bay, granted))
        session_id = self.database.start_session(
            bay_number, "simulator", bay["default_wash_mode"], purchased, granted, amount, estimated
        )
        self.database.add_fault(
            bay_number, "simulator", "purchased_granted_mismatch", "critical",
            f"Simulated payment covered {purchased} seconds, but the timer granted {granted} seconds. "
            "This is a demo mismatch; it does not establish a cause.",
            session_id, estimated,
        )
        self.database.update_bay(
            bay_number, {"motor_running": 1, "motor_remaining_s": float(granted), "motor_updated_at": time.time()}
        )

    def _sensor_fault(self, bay: dict, scenario: str) -> None:
        bay_number = bay["bay_number"]
        if bay["status"] == "faulted":
            raise ControllerError("Resolve this bay's open fault first", "conflict")
        if bay["status"] != "occupied":
            if not bay["enabled"]:
                raise ControllerError("Put this bay back in service first", "conflict")
            self._start_run(bay_number, min(180, bay["maximum_duration_seconds"]), bay["default_wash_mode"])
        bay = self.database.get_bay(bay_number)
        session_id = next((s["id"] for s in bay["sessions"] if s["status"] == "active"), None)
        if scenario == "motor_feedback_missing":
            self.database.update_bay(bay_number, {"motor_running": 0, "motor_remaining_s": 0})
            self.database.add_fault(
                bay_number, "simulator", scenario, "critical",
                "Motor control is active but simulated motor feedback is missing. "
                "This observation alone does not identify a root cause.",
                session_id,
            )
        else:
            self.database.update_bay(bay_number, {"water_detected": 0})
            self.database.add_fault(
                bay_number, "simulator", scenario, "warning",
                "The simulated wet-test cycle expected water, but its simulated water sensor reported none. "
                "This observation alone does not identify a root cause.",
                session_id,
            )

    def _session_overrun(self, bay: dict) -> None:
        if bay["status"] == "occupied":
            raise ControllerError("Wait for the current run to end before injecting an overrun", "conflict")
        if bay["status"] == "faulted":
            raise ControllerError("Resolve the active bay fault before injecting an overrun", "conflict")
        bay_number = bay["bay_number"]
        max_seconds = bay["maximum_duration_seconds"]
        session_id = self.database.start_session(
            bay_number, "simulator", bay["default_wash_mode"], max_seconds, max_seconds + 30,
            price_for_seconds(bay, max_seconds),
        )
        with self.database.connection() as db:
            db.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?", (time.time() - max_seconds - 2, session_id)
            )
        self.database.update_bay(
            bay_number, {"motor_running": 1, "motor_remaining_s": 28.0, "motor_updated_at": time.time()}
        )
        self.database.add_fault(
            bay_number, "simulator", "session_overrun", "critical",
            f"Simulated active session exceeded the configured {max_seconds} second limit. This is a test scenario.",
            session_id,
        )

    def _operator_fault(self, bay: dict) -> None:
        self.database.add_fault(
            bay["bay_number"], "simulator", "operator_test_fault", "warning",
            "Operator-injected simulator fault for a live demonstration. No hardware diagnosis is implied.",
        )

    # ------------------------------------------------------------------
    # background activity (every SIM_TICK_SECONDS, caller holds the lock)
    # ------------------------------------------------------------------
    def tick(self) -> None:
        now = time.time()
        for bay in self.database.list_bays():
            if bay["source"] != "simulator" or not bay["enabled"]:
                continue
            bay_number = bay["bay_number"]
            sessions = self.database.get_bay(bay_number)["sessions"]
            active = next((session for session in sessions if session["status"] == "active"), None)
            if active:
                self._tick_active(bay, active, now)
                continue
            if bay["status"] != "faulted" and self._random.random() < 0.002:
                seconds = min(self._random.choice(WASH_MINUTES) * 60, bay["maximum_duration_seconds"])
                self._start_run(bay_number, seconds, bay["default_wash_mode"])
            elif self._random.random() < 0.0002:
                self.database.add_reading(bay_number, "car", 1, "simulator")   # a car drives through

    def _tick_active(self, bay: dict, active: dict, now: float) -> None:
        bay_number = bay["bay_number"]
        elapsed = now - active["started_at"]
        if bay["motor_running"] == 1:
            self.database.update_bay(bay_number, {"motor_remaining_s": max(0, active["granted_seconds"] - elapsed)})
        if elapsed > bay["maximum_duration_seconds"]:
            self.database.add_fault(
                bay_number, "simulator", "session_duration_limit", "critical",
                f"Session has exceeded the configured maximum duration of {bay['maximum_duration_seconds']} "
                "seconds. The observation does not identify a cause.",
                active["id"],
            )
        if bay["motor_feedback_installed"] and bay["motor_running"] == 0:
            self.database.add_fault(
                bay_number, "simulator", "motor_feedback_missing", "critical",
                "Motor is commanded on but simulated feedback is missing. "
                "This observation alone does not identify a root cause.",
                active["id"],
            )
        if bay["water_sensor_installed"] and bay["motor_running"] and not bay["water_detected"]:
            self.database.add_fault(
                bay_number, "simulator", "water_not_detected", "warning",
                "Water was expected during this simulated wet-test cycle, but the simulated sensor reported none. "
                "This observation alone does not identify a root cause.",
                active["id"],
            )
        if self._random.random() < 0.0005:
            if self._random.choice(("motor_feedback_missing", "water_not_detected")) == "motor_feedback_missing":
                self.database.update_bay(bay_number, {"motor_running": 0, "motor_remaining_s": 0})
            else:
                self.database.update_bay(bay_number, {"water_detected": 0})
        if self._random.random() < 0.004:
            new_mode = self._random.choice([mode for mode in WASH_MODES if mode != bay["wash_mode"]])
            self.database.update_bay(bay_number, {"wash_mode": new_mode})
            with self.database.connection() as db:
                db.execute("UPDATE sessions SET wash_mode = ? WHERE id = ?", (new_mode, active["id"]))
        if elapsed >= active["granted_seconds"]:
            self.database.end_session(bay_number)
            self._finish_wash(bay_number)
