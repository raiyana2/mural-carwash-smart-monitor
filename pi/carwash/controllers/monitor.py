"""monitor.py: MonitorController, the dashboard's single entry point for all 11 bays.

    web (routes) ──> MonitorController ──┬──> HardwareBay ──> Esp32Link ──USB──> ESP32   (Bay 1)
                                         ├──> Simulator                                  (Bays 2-11)
                                         └──> MonitorDatabase (SQLite)

It owns the background worker (polls Bay 1, ticks the simulator), checks whether a
bay may run before handing the run to HardwareBay or Simulator, and builds the
dashboard view. When it refuses something it raises ControllerError.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from carwash.config import (
    BUTTON_RUN_SECONDS,
    HARDWARE_BAY,
    MAX_MOTOR_SECONDS,
    MOTOR_REST_ANGLE,
    MOTOR_RUN_ANGLE,
    POLL_SECONDS,
    SIM_TICK_SECONDS,
    WASH_MODES,
)
from carwash.controllers.errors import ControllerError
from carwash.controllers.hardware_bay import HardwareBay
from carwash.controllers.maintenance import EQUIPMENT, MaintenanceAnalyzer
from carwash.controllers.pricing import price_for_seconds
from carwash.controllers.simulator import Simulator
from carwash.storage.database import MonitorDatabase

log = logging.getLogger(__name__)


class MonitorController:
    def __init__(
        self,
        database: Optional[MonitorDatabase] = None,
        serial_port: Optional[str] = None,
        run_simulator: bool = True,
        seed_demo_data: bool = True,
        auto_connect: bool = True,
    ) -> None:
        self.database = database or MonitorDatabase()
        self.run_simulator = run_simulator
        self.seed_demo_data = seed_demo_data
        self._lock = threading.RLock()
        self.maintenance = MaintenanceAnalyzer(self.database)
        self.hardware = HardwareBay(self.database, self._lock, serial_port, auto_connect, self.maintenance)
        self.simulator = Simulator(self.database, self.start_session, self.stop_session, self.maintenance)
        self._stop = threading.Event()
        self._wake = threading.Event()        # set to run the worker loop right away
        self.hardware.wake = self._wake.set
        self.hardware.on_button = self._on_button_press
        self._worker: Optional[threading.Thread] = None
        self._next_sim_at = 0.0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self.database.initialize()
        if self.seed_demo_data:
            self.simulator.seed_demo_data()
            self.simulator.seed_history()
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="bay-monitor", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._worker:
            self._worker.join(timeout=3)
            self._worker = None
        self.hardware.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.hardware.maybe_auto_connect()   # opens the port without holding the lock
                with self._lock:
                    self.hardware.poll_locked()
                    if self.run_simulator and time.time() >= self._next_sim_at:
                        self.simulator.tick()
                        self._next_sim_at = time.time() + SIM_TICK_SECONDS
            except Exception:
                log.exception("Bay monitor iteration failed")
            # Sleep until the next poll, or until a button press wakes us early.
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def _on_button_press(self) -> None:
        """Bay 1's physical button: start a run, unless the bay can't run right now.
        Called by HardwareBay.poll_locked() with the lock held."""
        try:
            self.start_session(HARDWARE_BAY, BUTTON_RUN_SECONDS)
            result = f"Started a {BUTTON_RUN_SECONDS} s run"
        except ControllerError as exc:
            result = f"Ignored: {exc}"
        self.hardware.last_button_result = result
        log.info("Bay %s button pressed: %s", HARDWARE_BAY, result)

    # ------------------------------------------------------------------
    # reading state
    # ------------------------------------------------------------------
    def _decorate(self, bay: dict, hardware: dict, now: float) -> dict:
        bay["remaining_seconds"] = None
        bay["run_seconds"] = bay.get("active_granted_seconds")
        if bay["source"] == "hardware":
            return self.hardware.decorate(bay, hardware, now)
        return self.simulator.decorate(bay, now)

    def dashboard(self) -> dict:
        result = self.database.dashboard()
        hardware = self.hardware.status()
        now = time.time()
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        cars_today = self.database.count_readings_by_bay("car", midnight)
        last_flow = self.database.latest_reading_by_bay("flow")
        result["hardware"] = hardware
        result["bays"] = [self._decorate(bay, hardware, now) for bay in result["bays"]]
        for bay in result["bays"]:
            bay["cars_today"] = cars_today.get(bay["bay_number"], 0)
            flow = last_flow.get(bay["bay_number"])
            bay["last_wash_flow_lpm"] = flow["value"] if flow else None
        result["motor_run_angle"] = MOTOR_RUN_ANGLE
        result["motor_rest_angle"] = MOTOR_REST_ANGLE
        return result

    def bay_detail(self, bay_number: int) -> dict:
        bay = self._get_bay(bay_number)
        active = next((s for s in bay["sessions"] if s["status"] == "active"), None)
        bay["active_started_at"] = active["started_at"] if active else None
        bay["active_granted_seconds"] = active["granted_seconds"] if active else None
        return self._decorate(bay, self.hardware.status(), time.time())

    def _get_bay(self, bay_number: int) -> dict:
        bay = self.database.get_bay(bay_number)
        if bay is None:
            raise ControllerError("Bay not found", "not_found")
        return bay

    def analytics(self) -> dict:
        return self.database.analytics()

    def financial_report(self, days: int) -> dict:
        return self.database.financial_report(days)

    def maintenance_report(self) -> dict:
        return self.maintenance.report()

    def mark_serviced(self, bay_number: int, equipment: str) -> dict:
        """Record that a bay's water pump/nozzle, valve or car counter was serviced: its
        health history starts again and its alerts are cleared."""
        if equipment not in EQUIPMENT:
            raise ControllerError(f"Unknown equipment '{equipment}'", "invalid")
        with self._lock:
            self._get_bay(bay_number)
            self.maintenance.mark_serviced(bay_number, equipment)
        return self.maintenance_report()

    def recent_faults(self, limit: int = 100) -> list[dict]:
        return self.database.recent_faults(limit)

    # ------------------------------------------------------------------
    # Bay 1 hardware
    # ------------------------------------------------------------------
    def serial_ports(self) -> list[dict]:
        return self.hardware.serial_ports()

    def connect_hardware(self, port: Optional[str]) -> dict:
        self.hardware.connect(port)
        return self.bay_detail(HARDWARE_BAY)

    def disconnect_hardware(self) -> dict:
        self.hardware.disconnect()
        return {"connected": False, "auto_connect": False}

    def ping_hardware(self) -> dict:
        return self.hardware.ping()

    # ------------------------------------------------------------------
    # motor runs
    # ------------------------------------------------------------------
    def start_session(self, bay_number: int, seconds: int, mode: Optional[str] = None) -> dict:
        """Run a bay's motor: 180° for `seconds`, then back to 0°."""
        with self._lock:
            bay = self._get_bay(bay_number)
            if not bay["enabled"]:
                raise ControllerError(f"{bay['label']} is out of service. Put it back in service on the Maintenance page.", "conflict")
            if bay["status"] == "faulted":
                raise ControllerError(f"{bay['label']} has an open fault. Resolve it on the Maintenance page first.", "conflict")
            if any(session["status"] == "active" for session in bay["sessions"]):
                raise ControllerError(f"{bay['label']}'s motor is already running", "conflict")
            limit = min(bay["maximum_duration_seconds"], MAX_MOTOR_SECONDS)
            if seconds > limit:
                raise ControllerError(f"Run time can be at most {limit} seconds for {bay['label']}", "invalid")
            if mode and mode not in WASH_MODES:
                raise ControllerError("Unsupported wash mode", "invalid")
            wash_mode = mode or bay["default_wash_mode"]
            if bay["source"] == "hardware":
                session_id = self.hardware.start_run_locked(bay, seconds, wash_mode)
            else:
                session_id = self.simulator.start_run(bay, seconds, wash_mode)
            return {"session_id": session_id, **self.bay_detail(bay_number)}

    def stop_session(self, bay_number: int) -> dict:
        """Turn a bay's motor back to 0° now."""
        with self._lock:
            bay = self._get_bay(bay_number)
            if bay["source"] == "hardware":
                self.hardware.stop_run_locked()
            else:
                self.simulator.stop_run(bay_number)
            return self.bay_detail(bay_number)

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def update_settings(self, bay_number: int, values: dict) -> dict:
        with self._lock:
            bay = self._get_bay(bay_number)
            if values["default_wash_mode"] not in WASH_MODES:
                raise ControllerError("Unsupported wash mode", "invalid")
            if values["maximum_duration_seconds"] < values["included_seconds"]:
                raise ControllerError("Maximum wash duration cannot be shorter than the included wash time", "invalid")
            if not values["enabled"] and bay["status"] == "occupied":
                raise ControllerError("End the active wash before disabling this bay", "conflict")
            self.database.update_settings(bay_number, {**values, "enabled": int(values["enabled"])})
            return self.bay_detail(bay_number)

    def set_enabled(self, bay_number: int, enabled: bool) -> dict:
        """Put a bay in or out of service."""
        with self._lock:
            bay = self._get_bay(bay_number)
            if not enabled and (bay["status"] == "occupied" or bay["motor_running"]):
                raise ControllerError(
                    "Wait for the wash to finish (or press Stop wash) before taking this bay out of service",
                    "conflict",
                )
            self.database.update_bay(bay_number, {"enabled": int(enabled)})
            return self.bay_detail(bay_number)

    def preview(self, bay_number: int, seconds: int) -> dict:
        bay = self._get_bay(bay_number)
        if seconds > bay["maximum_duration_seconds"]:
            raise ControllerError(
                f"Requested wash exceeds this bay's {bay['maximum_duration_seconds']} second maximum", "invalid"
            )
        return {"seconds": seconds, "price_cents": price_for_seconds(bay, seconds)}

    # ------------------------------------------------------------------
    # faults and the simulator
    # ------------------------------------------------------------------
    def trigger_scenario(self, bay_number: int, scenario: str) -> dict:
        with self._lock:
            self.simulator.trigger(bay_number, scenario)
            return self.bay_detail(bay_number)

    def resolve_fault(self, fault_id: int) -> dict:
        with self._lock:
            bay_number = self.database.resolve_fault(fault_id)
            if bay_number is None:
                raise ControllerError("Active fault not found", "not_found")
            return self.bay_detail(bay_number)
