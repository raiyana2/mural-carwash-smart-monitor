"""simulator.py: Bays 2-11, which pretend to be bays like Bay 1.

The simulator mirrors Bay 1's run (motor to 180°, hold, back to 0°) in the database,
starts occasional runs on its own, raises occasional faults, and injects labelled
test faults from the Maintenance page. It never touches the serial port.

It starts and stops runs through the callables MonitorController gives it, so its
runs go through the same checks (in service, no open fault, time limit) as yours.
"""

from __future__ import annotations

import random
import time
from typing import Callable, Optional

from carwash.config import MOTOR_REST_ANGLE, MOTOR_RUN_ANGLE, WASH_MODES
from carwash.controllers.errors import ControllerError
from carwash.controllers.pricing import price_for_seconds
from carwash.storage.database import MonitorDatabase


class Simulator:
    def __init__(
        self,
        database: MonitorDatabase,
        start_run: Callable[[int, int, Optional[str]], dict],
        stop_run: Callable[[int], dict],
        rng: Optional[random.Random] = None,
    ) -> None:
        self.database = database
        self._start_run = start_run    # MonitorController.start_session
        self._stop_run = stop_run      # MonitorController.stop_session
        self._random = rng or random.Random()

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
        return session_id

    def stop_run(self, bay_number: int) -> None:
        self.database.end_session(bay_number, "completed")
        self.database.update_bay(bay_number, {"motor_running": 0, "motor_remaining_s": 0})

    @staticmethod
    def decorate(bay: dict, now: float) -> dict:
        bay["connected"] = True
        bay["connected_port"] = None
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
            if bay["status"] != "faulted" and self._random.random() < 0.003:
                shortest = min(90, bay["maximum_duration_seconds"])
                longest = min(240, bay["maximum_duration_seconds"])
                self._start_run(bay_number, self._random.randint(shortest, longest), bay["default_wash_mode"])

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
