"""Local multi-bay dashboard and the single owner of the Bay 1 serial link.

Bay 1 is the physical ESP32: a positional servo and a rain sensor. A "run" turns
the servo 180° (from 0° to 180°), holds it for the chosen number of seconds, then
the ESP32 turns it back to 0° on its own timer. Bays 2–11 simulate the same thing.
"""

from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import random
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import serial

from database import MonitorDatabase
from esp32_link import Esp32ConnectError, Esp32Error, Esp32Link, list_serial_ports

STATIC_DIR = Path(__file__).parent / "static"
MAX_MOTOR_SECONDS = 300
MOTOR_REST_ANGLE = 0      # where the servo waits between runs
MOTOR_RUN_ANGLE = 180     # where a run turns it to
HARDWARE_STALE_SECONDS = 10
RAIN_STALE_SECONDS = 15
WASH_MODES = ("Standard", "Rinse", "Foam", "Wax")
# How long to wait before auto-connecting again after a failed attempt. A port that is
# present but silent is retried slowly, so the dashboard doesn't keep the port open and
# get in the way of a PlatformIO upload or serial monitor.
RETRY_SECONDS = {"not_found": 3, "missing_port": 5, "busy": 10, "permission": 30, "no_answer": 20, "old_firmware": 60}
RETRY_SECONDS_DEFAULT = 10
LINK_ERRORS = (TimeoutError, serial.SerialException, OSError, RuntimeError)
log = logging.getLogger(__name__)


class ConnectRequest(BaseModel):
    port: Optional[str] = None


class SessionRequest(BaseModel):
    seconds: int = Field(ge=1, le=MAX_MOTOR_SECONDS)
    wash_mode: Optional[str] = None


class SettingsRequest(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    enabled: bool
    default_wash_mode: str
    base_price_cents: int = Field(ge=0, le=100000)
    included_seconds: int = Field(ge=0, le=MAX_MOTOR_SECONDS)
    additional_price_per_minute_cents: int = Field(ge=0, le=100000)
    maximum_duration_seconds: int = Field(ge=1, le=MAX_MOTOR_SECONDS)


class EnabledRequest(BaseModel):
    enabled: bool


class PreviewRequest(BaseModel):
    bay_number: int = Field(ge=1, le=11)
    seconds: int = Field(ge=1, le=MAX_MOTOR_SECONDS)


class ScenarioRequest(BaseModel):
    bay_number: int = Field(ge=2, le=11)
    scenario: str


def price_for_seconds(settings: dict, seconds: int) -> int:
    extra_seconds = max(0, seconds - settings["included_seconds"])
    extra_minutes = (extra_seconds + 59) // 60
    return settings["base_price_cents"] + (
        extra_minutes * settings["additional_price_per_minute_cents"]
    )


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
        self.serial_port = serial_port
        self.run_simulator = run_simulator
        self.seed_demo_data = seed_demo_data
        self._link: Optional[Esp32Link] = None
        self._link_source = "hardware"
        self._lock = threading.RLock()       # guards the link and Bay 1 state
        self._open_lock = threading.Lock()   # only one attempt to open the port at a time
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._ever_connected = False
        self._auto_connect = auto_connect    # paused by Disconnect, resumed by Connect
        self._next_connect_at = 0.0
        self._next_rain_at = 0.0
        self._next_sim_at = 0.0
        self._last_connection_error: Optional[str] = None
        self._last_connection_error_kind: Optional[str] = None
        self._servo_angle: Optional[int] = None  # last angle reported by the ESP32 (Bay 1)
        self._esp_restarted = False              # set by the reader thread on a "ready" event
        self._random = random.Random()

    def start(self) -> None:
        self.database.initialize()
        if self.seed_demo_data:
            self._seed_demo_data()
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="bay-monitor", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=3)
            self._worker = None
        with self._lock:
            self._close_link_locked()

    def _seed_demo_data(self) -> None:
        with self.database.connection() as db:
            if db.execute(
                "SELECT COUNT(*) FROM sessions WHERE source = 'simulator'"
            ).fetchone()[0]:
                return
        now = time.time()
        for bay_number, age in ((3, 3600), (4, 2400), (6, 1200)):
            bay = self.database.get_bay(bay_number)
            price = price_for_seconds(bay, 120)
            session_id = self.database.start_session(
                bay_number, "simulator", "Standard", 120, 120, price
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
        price = price_for_seconds(bay, 180)
        session_id = self.database.start_session(
            2, "simulator", "Foam", 180, 180, price
        )
        with self.database.connection() as db:
            db.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (now - 45, session_id),
            )
            db.execute(
                "UPDATE bays SET motor_running = 1, motor_remaining_s = 135, motor_updated_at = ? WHERE bay_number = 2",
                (now,),
            )

    # ------------------------------------------------------------------
    # background worker
    # ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._link is None:
                    self._maybe_auto_connect()   # opens the port without holding _lock
                with self._lock:
                    self._poll_hardware_locked()
                    if self.run_simulator and time.time() >= self._next_sim_at:
                        self._tick_simulator_locked()
                        self._next_sim_at = time.time() + 2
            except Exception:
                log.exception("Bay monitor iteration failed")
            self._stop.wait(1)

    # ------------------------------------------------------------------
    # connecting
    # ------------------------------------------------------------------
    def _port_to_use(self) -> Optional[str]:
        return self.serial_port or os.getenv("ESP32_PORT") or None

    def _on_esp_event(self, event: dict) -> None:
        # Runs on the serial reader thread; the worker handles it under the lock.
        if event.get("event") == "ready":
            self._esp_restarted = True

    def _open_link(self) -> Esp32Link:
        """Open and verify the ESP32. Raises Esp32ConnectError and records why it failed."""
        link = Esp32Link(port=self._port_to_use(), on_event=self._on_esp_event)
        try:
            link.open()
        except Esp32ConnectError as exc:
            self._record_connect_failure(str(exc), exc.kind)
            raise
        except (Esp32Error, *LINK_ERRORS) as exc:
            link.close()
            message = f"The ESP32 answered, then a status request failed: {exc}"
            self._record_connect_failure(message, "open_failed")
            raise Esp32ConnectError(message, "open_failed", link.port) from exc
        return link

    def _record_connect_failure(self, message: str, kind: str) -> None:
        self._last_connection_error = message
        self._last_connection_error_kind = kind
        self._next_connect_at = time.time() + RETRY_SECONDS.get(kind, RETRY_SECONDS_DEFAULT)

    def _maybe_auto_connect(self) -> None:
        if not self._auto_connect or time.time() < self._next_connect_at:
            return
        if not self._open_lock.acquire(blocking=False):
            return  # a manual Connect is already opening the port
        try:
            if self._link is not None or not self._auto_connect:
                return
            try:
                link = self._open_link()
            except Esp32ConnectError:
                return
            with self._lock:
                if self._auto_connect and self._link is None:
                    self._install_link_locked(link)
                    return
            link.close()
        finally:
            self._open_lock.release()

    def _install_link_locked(self, link: Esp32Link) -> None:
        self._link = link
        self._link_source = self._source_for_port(link.port)
        self._ever_connected = True
        self._last_connection_error = None
        self._last_connection_error_kind = None
        self._esp_restarted = False   # a reset while opening isn't news
        self._next_rain_at = 0.0
        self._servo_angle = link.angle
        self.database.resolve_faults(1, "serial_connection_lost")

    def connect_hardware(self, port: Optional[str]) -> dict:
        with self._open_lock:
            with self._lock:
                self._close_link_locked()
                self.serial_port = (port or "").strip() or None
                self._auto_connect = True
            try:
                link = self._open_link()
            except Esp32ConnectError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            with self._lock:
                self._install_link_locked(link)
                self._poll_hardware_locked()
                if self._link is None:
                    raise HTTPException(
                        status_code=503,
                        detail=f"The ESP32 on {link.port} answered once, then stopped: {self._last_connection_error or 'no reply'}",
                    )
                return self.bay_detail(1)

    def disconnect_hardware(self) -> dict:
        with self._lock:
            # Stay disconnected until Connect is pressed, so other programs
            # (a PlatformIO upload, a serial monitor) can use the port.
            self._auto_connect = False
            self._close_link_locked()
            self._mark_bay1_offline_locked()
            return {"connected": False, "auto_connect": False}

    def ping_hardware(self) -> dict:
        with self._lock:
            link = self._require_link_locked()
            try:
                uptime = link.ping()
                motor = link.motor_status()
            except Esp32Error as exc:
                raise HTTPException(status_code=409, detail=f"The ESP32 refused: {exc}") from exc
            except LINK_ERRORS as exc:
                self._connection_lost_locked(exc)
                raise HTTPException(status_code=503, detail=f"ESP32 ping failed: {exc}") from exc
            self._record_motor_locked(motor)
            return {"uptime_s": uptime, "connected": True}

    @staticmethod
    def _source_for_port(port: Optional[str]) -> str:
        return "simulator" if port and port.startswith("/dev/pts/") else "hardware"

    def _require_link_locked(self) -> Esp32Link:
        if self._link is None or not self._hardware_status()["connected"]:
            raise HTTPException(
                status_code=503,
                detail="Bay 1's ESP32 isn't connected. Connect it on the Maintenance page.",
            )
        return self._link

    # ------------------------------------------------------------------
    # polling Bay 1
    # ------------------------------------------------------------------
    def _poll_hardware_locked(self) -> None:
        link = self._link
        if link is None:
            return
        if not link.connected:
            self._connection_lost_locked(RuntimeError("the USB serial port closed (cable unplugged?)"))
            return
        try:
            motor = link.motor_status()
            reading = None
            if time.time() >= self._next_rain_at:
                reading = link.read_rain()
                self._next_rain_at = time.time() + 5
        except Esp32Error as exc:
            # It answered, so the link is fine; just note the refusal.
            log.warning("ESP32 refused a status request: %s", exc)
            return
        except LINK_ERRORS as exc:
            self._connection_lost_locked(exc)
            return
        if reading is not None:
            self.database.update_bay(
                1,
                {
                    "rain_detected": int(reading.raining),
                    "rain_raw": reading.raw,
                    "rain_wet_percent": reading.wet_percent,
                    "rain_updated_at": time.time(),
                },
            )
        self._record_motor_locked(motor)

    def _record_motor_locked(self, motor) -> None:
        """Store the ESP32's motor state for Bay 1 and end a run the ESP32 has finished."""
        now = time.time()
        self._servo_angle = motor.angle
        bay = self.database.get_bay(1)
        active = next((s for s in bay["sessions"] if s["status"] == "active"), None)
        if self._esp_restarted:
            self._esp_restarted = False
            self.database.add_fault(
                1,
                "hardware",
                "esp32_restarted",
                "warning",
                "The ESP32 restarted on its own"
                + (", ending the motor run early" if active else "")
                + ". A common cause is powering the servo from the board's USB 5 V (it browns out "
                "when the servo moves); give the servo its own 5 V supply with a shared ground.",
                active["id"] if active else None,
            )
        if active and not motor.running:
            self.database.end_session(1)
        critical = any(f["active"] and f["severity"] == "critical" for f in bay["faults"])
        self.database.update_bay(
            1,
            {
                "last_seen_at": now,
                "connection_source": self._link_source,
                "motor_running": int(motor.running),
                "motor_remaining_s": motor.remaining_s,
                "motor_updated_at": now,
                "status": "occupied" if motor.running else ("faulted" if critical else "available"),
            },
        )
        self._last_connection_error = None
        self._last_connection_error_kind = None

    def _connection_lost_locked(self, error: Exception) -> None:
        was_connected = self._ever_connected
        port = self._link.port if self._link else self._port_to_use()
        self._close_link_locked()
        self._mark_bay1_offline_locked()
        self._last_connection_error = f"Lost the ESP32 on {port}: {error}"
        self._last_connection_error_kind = "lost"
        self._next_connect_at = time.time() + 3
        if was_connected:
            self.database.add_fault(
                1,
                "hardware",
                "serial_connection_lost",
                "warning",
                f"ESP32 serial communication stopped responding: {error}. Sensor values are unavailable until reconnection.",
            )

    def _mark_bay1_offline_locked(self) -> None:
        self.database.update_bay(
            1,
            {
                "status": "offline",
                "motor_running": None,
                "motor_remaining_s": None,
                "motor_updated_at": None,
                "last_seen_at": None,
            },
        )

    def _close_link_locked(self) -> None:
        self._servo_angle = None
        if self._link is not None:
            self._link.close()
            self._link = None

    # ------------------------------------------------------------------
    # reading state
    # ------------------------------------------------------------------
    def _hardware_status(self) -> dict:
        bay = self.database.get_bay(1)
        connected = bool(
            self._link
            and bay
            and bay["last_seen_at"]
            and time.time() - bay["last_seen_at"] <= HARDWARE_STALE_SECONDS
        )
        retry_in = None
        if not connected and self._auto_connect:
            retry_in = max(0, round(self._next_connect_at - time.time()))
        return {
            "connected": connected,
            "port": self._link.port if connected and self._link else self._port_to_use(),
            "last_error": None if connected else self._last_connection_error,
            "last_error_kind": None if connected else self._last_connection_error_kind,
            "last_seen_at": bay["last_seen_at"] if bay else None,
            "auto_connect": self._auto_connect,
            "retry_in_s": retry_in,
        }

    def _decorate_bay(self, bay: dict, hardware: dict, now: float) -> dict:
        """Add live fields the UI needs: connection, servo angle, and time left in the run."""
        bay["remaining_seconds"] = None
        bay["run_seconds"] = bay.get("active_granted_seconds")
        if bay["source"] == "hardware":
            bay["connected"] = hardware["connected"]
            bay["connected_port"] = hardware["port"]
            if not bay["rain_updated_at"] or now - bay["rain_updated_at"] > RAIN_STALE_SECONDS or not hardware["connected"]:
                bay["rain_detected"] = None
                bay["rain_raw"] = None
                bay["rain_wet_percent"] = None
            if not bay["motor_updated_at"] or now - bay["motor_updated_at"] > HARDWARE_STALE_SECONDS or not hardware["connected"]:
                bay["motor_running"] = None
                bay["motor_remaining_s"] = None
                bay["servo_angle"] = None
            else:
                bay["servo_angle"] = self._servo_angle
                if bay["motor_running"]:
                    # The ESP32's own timer is the truth; allow for the time since the last poll.
                    bay["remaining_seconds"] = max(
                        0, round(bay["motor_remaining_s"] - (now - bay["motor_updated_at"]))
                    )
        else:
            bay["connected"] = True
            bay["connected_port"] = None
            bay["servo_angle"] = MOTOR_RUN_ANGLE if bay["motor_running"] else MOTOR_REST_ANGLE
            if bay.get("active_started_at") is not None:
                elapsed = max(0, int(now - bay["active_started_at"]))
                bay["remaining_seconds"] = max(0, bay["active_granted_seconds"] - elapsed)
        return bay

    def dashboard(self) -> dict:
        result = self.database.dashboard()
        hardware = self._hardware_status()
        now = time.time()
        result["hardware"] = hardware
        result["bays"] = [self._decorate_bay(bay, hardware, now) for bay in result["bays"]]
        result["motor_run_angle"] = MOTOR_RUN_ANGLE
        result["motor_rest_angle"] = MOTOR_REST_ANGLE
        return result

    def bay_detail(self, bay_number: int) -> dict:
        bay = self.database.get_bay(bay_number)
        if bay is None:
            raise HTTPException(status_code=404, detail="Bay not found")
        active = next((s for s in bay["sessions"] if s["status"] == "active"), None)
        bay["active_started_at"] = active["started_at"] if active else None
        bay["active_granted_seconds"] = active["granted_seconds"] if active else None
        return self._decorate_bay(bay, self._hardware_status(), time.time())

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def update_settings(self, bay_number: int, values: dict) -> dict:
        with self._lock:
            return self._update_settings(bay_number, values)

    def _update_settings(self, bay_number: int, values: dict) -> dict:
        if self.database.get_bay(bay_number) is None:
            raise HTTPException(status_code=404, detail="Bay not found")
        if values["default_wash_mode"] not in WASH_MODES:
            raise HTTPException(status_code=422, detail="Unsupported wash mode")
        if values["maximum_duration_seconds"] < values["included_seconds"]:
            raise HTTPException(
                status_code=422,
                detail="Maximum wash duration cannot be shorter than the included wash time",
            )
        if not values["enabled"] and self.database.get_bay(bay_number)["status"] == "occupied":
            raise HTTPException(status_code=409, detail="End the active wash before disabling this bay")
        self.database.update_settings(bay_number, {**values, "enabled": int(values["enabled"])})
        return self.bay_detail(bay_number)

    def set_enabled(self, bay_number: int, enabled: bool) -> dict:
        """Put a bay in or out of service (Maintenance page)."""
        with self._lock:
            bay = self.database.get_bay(bay_number)
            if bay is None:
                raise HTTPException(status_code=404, detail="Bay not found")
            if not enabled and (bay["status"] == "occupied" or bay["motor_running"]):
                raise HTTPException(status_code=409, detail="Wait for the run to finish (or press Return now) before taking this bay out of service")
            self.database.update_bay(bay_number, {"enabled": int(enabled)})
            return self.bay_detail(bay_number)

    def preview(self, bay_number: int, seconds: int) -> dict:
        bay = self.database.get_bay(bay_number)
        if bay is None:
            raise HTTPException(status_code=404, detail="Bay not found")
        if seconds > bay["maximum_duration_seconds"]:
            raise HTTPException(
                status_code=422,
                detail=f"Requested wash exceeds this bay's {bay['maximum_duration_seconds']} second maximum",
            )
        return {"seconds": seconds, "price_cents": price_for_seconds(bay, seconds)}

    # ------------------------------------------------------------------
    # motor runs
    # ------------------------------------------------------------------
    def start_session(self, bay_number: int, seconds: int, mode: Optional[str]) -> dict:
        with self._lock:
            return self._start_session(bay_number, seconds, mode)

    def _start_session(self, bay_number: int, seconds: int, mode: Optional[str]) -> dict:
        bay = self.database.get_bay(bay_number)
        if bay is None:
            raise HTTPException(status_code=404, detail="Bay not found")
        if not bay["enabled"]:
            raise HTTPException(status_code=409, detail=f"{bay['label']} is out of service. Put it back in service on the Maintenance page.")
        if bay["status"] == "faulted":
            raise HTTPException(status_code=409, detail=f"{bay['label']} has an open fault. Resolve it on the Maintenance page first.")
        if any(session["status"] == "active" for session in bay["sessions"]):
            raise HTTPException(status_code=409, detail=f"{bay['label']}'s motor is already running")
        limit = min(bay["maximum_duration_seconds"], MAX_MOTOR_SECONDS)
        if seconds > limit:
            raise HTTPException(status_code=422, detail=f"Run time can be at most {limit} seconds for {bay['label']}")
        if mode and mode not in WASH_MODES:
            raise HTTPException(status_code=422, detail="Unsupported wash mode")
        wash_mode = mode or bay["default_wash_mode"]
        if bay["source"] == "hardware":
            link = self._require_link_locked()
            try:
                status = link.motor_status()
                if status.running:
                    raise HTTPException(status_code=409, detail="Bay 1's motor is already running. Press Return now first.")
                if status.angle is not None and status.angle != MOTOR_REST_ANGLE:
                    # Start from rest so a run is always the full 0° -> 180° turn, and the
                    # ESP32 turns it back to 0° when the timer ends.
                    try:
                        link.servo_set(MOTOR_REST_ANGLE)
                    except Esp32Error:
                        pass  # older firmware without servo_set returns to rest by itself
                motor = link.motor_on(seconds, angle=MOTOR_RUN_ANGLE)
            except HTTPException:
                raise
            except Esp32Error as exc:
                # The ESP32 answered and said no: the link is fine, so don't drop it.
                raise HTTPException(status_code=409, detail=f"The ESP32 refused the run: {exc}") from exc
            except LINK_ERRORS as exc:
                self._connection_lost_locked(exc)
                raise HTTPException(status_code=503, detail=f"Bay 1 motor command failed: {exc}") from exc
            session_id = self.database.start_session(
                bay_number, self._link_source, wash_mode, seconds, seconds, price_for_seconds(bay, seconds)
            )
            self._record_motor_locked(motor)
            return {"session_id": session_id, **self.bay_detail(1)}
        try:
            session_id = self.database.start_session(
                bay_number, "simulator", wash_mode, seconds, seconds, price_for_seconds(bay, seconds)
            )
            self.database.update_bay(
                bay_number,
                {"motor_running": 1, "motor_remaining_s": float(seconds), "motor_updated_at": time.time()},
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"session_id": session_id, **self.bay_detail(bay_number)}

    def stop_session(self, bay_number: int) -> dict:
        with self._lock:
            return self._stop_session(bay_number)

    def _stop_session(self, bay_number: int) -> dict:
        """Turn the motor back now. Works whenever it's running, even with no recorded run
        (for example one started before the dashboard restarted)."""
        bay = self.database.get_bay(bay_number)
        if bay is None:
            raise HTTPException(status_code=404, detail="Bay not found")
        if bay["source"] == "hardware":
            link = self._require_link_locked()
            try:
                motor = link.motor_off()
            except Esp32Error as exc:
                raise HTTPException(status_code=409, detail=f"The ESP32 refused to stop: {exc}") from exc
            except LINK_ERRORS as exc:
                self._connection_lost_locked(exc)
                raise HTTPException(status_code=503, detail=f"Bay 1 motor stop failed: {exc}") from exc
            self.database.end_session(bay_number, "completed")
            self._record_motor_locked(motor)
        else:
            self.database.end_session(bay_number, "completed")
            self.database.update_bay(bay_number, {"motor_running": 0, "motor_remaining_s": 0})
        return self.bay_detail(bay_number)

    # ------------------------------------------------------------------
    # simulator
    # ------------------------------------------------------------------
    def trigger_scenario(self, bay_number: int, scenario: str) -> dict:
        with self._lock:
            return self._trigger_scenario(bay_number, scenario)

    def _trigger_scenario(self, bay_number: int, scenario: str) -> dict:
        bay = self.database.get_bay(bay_number)
        if bay is None or bay["source"] != "simulator":
            raise HTTPException(status_code=422, detail="Scenarios can only target simulated Bays 2–11")
        if scenario == "arrival":
            if bay["status"] == "occupied":
                raise HTTPException(status_code=409, detail="Bay already has an active session")
            self.start_session(bay_number, min(180, bay["maximum_duration_seconds"]), bay["default_wash_mode"])
        elif scenario == "departure":
            if bay["status"] != "occupied":
                raise HTTPException(status_code=409, detail="Bay has no active session")
            self.stop_session(bay_number)
        elif scenario == "mode_change":
            mode = next(mode for mode in WASH_MODES if mode != bay["wash_mode"])
            self.database.update_bay(bay_number, {"wash_mode": mode, "default_wash_mode": mode})
            with self.database.connection() as db:
                db.execute(
                    "UPDATE sessions SET wash_mode = ? WHERE bay_number = ? AND status = 'active'",
                    (mode, bay_number),
                )
        elif scenario == "pricing_anomaly":
            if bay["status"] == "occupied":
                raise HTTPException(status_code=409, detail="End the current session before injecting an anomaly")
            if bay["status"] == "faulted":
                raise HTTPException(status_code=409, detail="Resolve the active bay fault before injecting a pricing anomaly")
            if bay["maximum_duration_seconds"] < 31:
                raise HTTPException(
                    status_code=409,
                    detail="Increase this simulator bay's maximum to at least 31 seconds to demonstrate a time mismatch",
                )
            purchased, granted = min(300, bay["maximum_duration_seconds"]), 30
            amount = price_for_seconds(bay, purchased)
            estimated = max(0, amount - price_for_seconds(bay, granted))
            session_id = self.database.start_session(
                bay_number, "simulator", bay["default_wash_mode"], purchased, granted,
                amount, estimated,
            )
            self.database.add_fault(
                bay_number,
                "simulator",
                "purchased_granted_mismatch",
                "critical",
                f"Simulated payment covered {purchased} seconds, but the timer granted {granted} seconds. This is a demo mismatch; it does not establish a cause.",
                session_id,
                estimated,
            )
            self.database.update_bay(
                bay_number,
                {"motor_running": 1, "motor_remaining_s": float(granted), "motor_updated_at": time.time()},
            )
        elif scenario in ("motor_feedback_missing", "water_not_detected"):
            if bay["status"] == "faulted":
                raise HTTPException(status_code=409, detail="Resolve this bay's open fault first")
            if bay["status"] != "occupied":
                if not bay["enabled"]:
                    raise HTTPException(status_code=409, detail="Put this bay back in service first")
                self.start_session(
                    bay_number, min(180, bay["maximum_duration_seconds"]), bay["default_wash_mode"]
                )
            bay = self.database.get_bay(bay_number)
            if scenario == "motor_feedback_missing":
                self.database.update_bay(bay_number, {"motor_running": 0, "motor_remaining_s": 0})
                self.database.add_fault(
                    bay_number, "simulator", scenario, "critical",
                    "Motor control is active but simulated motor feedback is missing. This observation alone does not identify a root cause.",
                    next((s["id"] for s in bay["sessions"] if s["status"] == "active"), None),
                )
            else:
                self.database.update_bay(bay_number, {"water_detected": 0})
                self.database.add_fault(
                    bay_number, "simulator", scenario, "warning",
                    "The simulated wet-test cycle expected water, but its simulated water sensor reported none. This observation alone does not identify a root cause.",
                    next((s["id"] for s in bay["sessions"] if s["status"] == "active"), None),
                )
        elif scenario == "session_overrun":
            if bay["status"] == "occupied":
                raise HTTPException(status_code=409, detail="Wait for the current run to end before injecting an overrun")
            if bay["status"] == "faulted":
                raise HTTPException(status_code=409, detail="Resolve the active bay fault before injecting an overrun")
            max_seconds = bay["maximum_duration_seconds"]
            session_id = self.database.start_session(
                bay_number, "simulator", bay["default_wash_mode"],
                max_seconds, max_seconds + 30,
                price_for_seconds(bay, max_seconds),
            )
            with self.database.connection() as db:
                db.execute(
                    "UPDATE sessions SET started_at = ? WHERE id = ?",
                    (time.time() - max_seconds - 2, session_id),
                )
            self.database.update_bay(
                bay_number, {"motor_running": 1, "motor_remaining_s": 28.0, "motor_updated_at": time.time()}
            )
            self.database.add_fault(
                bay_number, "simulator", scenario, "critical",
                f"Simulated active session exceeded the configured {max_seconds} second limit. This is a test scenario.",
                session_id,
            )
        elif scenario == "fault":
            self.database.add_fault(
                bay_number, "simulator", "operator_test_fault", "warning",
                "Operator-injected simulator fault for a live demonstration. No hardware diagnosis is implied.",
            )
        else:
            raise HTTPException(status_code=422, detail="Unknown simulator scenario")
        return self.bay_detail(bay_number)

    def _tick_simulator_locked(self) -> None:
        now = time.time()
        for bay in self.database.list_bays()[1:]:
            bay_number = bay["bay_number"]
            if not bay["enabled"]:
                continue
            sessions = self.database.get_bay(bay_number)["sessions"]
            active = next((session for session in sessions if session["status"] == "active"), None)
            if active:
                elapsed = now - active["started_at"]
                if bay["motor_running"] == 1:
                    self.database.update_bay(
                        bay_number,
                        {"motor_remaining_s": max(0, active["granted_seconds"] - elapsed)},
                    )
                if elapsed > bay["maximum_duration_seconds"]:
                    self.database.add_fault(
                        bay_number,
                        "simulator",
                        "session_duration_limit",
                        "critical",
                        f"Session has exceeded the configured maximum duration of {bay['maximum_duration_seconds']} seconds. The observation does not identify a cause.",
                        active["id"],
                    )
                if bay["motor_feedback_installed"] and bay["motor_running"] == 0:
                    self.database.add_fault(
                        bay_number,
                        "simulator",
                        "motor_feedback_missing",
                        "critical",
                        "Motor is commanded on but simulated feedback is missing. This observation alone does not identify a root cause.",
                        active["id"],
                    )
                if bay["water_sensor_installed"] and bay["motor_running"] and not bay["water_detected"]:
                    self.database.add_fault(
                        bay_number,
                        "simulator",
                        "water_not_detected",
                        "warning",
                        "Water was expected during this simulated wet-test cycle, but the simulated sensor reported none. This observation alone does not identify a root cause.",
                        active["id"],
                    )
                if self._random.random() < 0.0005:
                    if self._random.choice(("motor_feedback_missing", "water_not_detected")) == "motor_feedback_missing":
                        self.database.update_bay(
                            bay_number, {"motor_running": 0, "motor_remaining_s": 0}
                        )
                    else:
                        self.database.update_bay(bay_number, {"water_detected": 0})
                if self._random.random() < 0.004:
                    new_mode = self._random.choice(
                        [mode for mode in WASH_MODES if mode != bay["wash_mode"]]
                    )
                    self.database.update_bay(bay_number, {"wash_mode": new_mode})
                    with self.database.connection() as db:
                        db.execute(
                            "UPDATE sessions SET wash_mode = ? WHERE id = ?",
                            (new_mode, active["id"]),
                        )
                if elapsed >= active["granted_seconds"]:
                    self.database.end_session(bay_number)
                continue
            if bay["status"] != "faulted" and self._random.random() < 0.003:
                shortest = min(90, bay["maximum_duration_seconds"])
                longest = min(240, bay["maximum_duration_seconds"])
                seconds = self._random.randint(shortest, longest)
                self.start_session(bay_number, seconds, bay["default_wash_mode"])

    def analytics(self) -> dict:
        return self.database.analytics()

    def resolve_fault(self, fault_id: int) -> dict:
        with self._lock:
            bay_number = self.database.resolve_fault(fault_id)
            if bay_number is None:
                raise HTTPException(status_code=404, detail="Active fault not found")
            return self.bay_detail(bay_number)


controller = MonitorController()


@asynccontextmanager
async def lifespan(_: FastAPI):
    controller.start()
    try:
        yield
    finally:
        controller.close()


app = FastAPI(title="Mural Car Wash Smart Monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/dashboard")
def dashboard() -> dict:
    return controller.dashboard()


@app.get("/api/bays/{bay_number}")
def bay_detail(bay_number: int) -> dict:
    return controller.bay_detail(bay_number)


@app.put("/api/bays/{bay_number}/settings")
def update_settings(bay_number: int, request: SettingsRequest) -> dict:
    values = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    return controller.update_settings(bay_number, values)


@app.put("/api/bays/{bay_number}/enabled")
def set_enabled(bay_number: int, request: EnabledRequest) -> dict:
    return controller.set_enabled(bay_number, request.enabled)


@app.post("/api/bays/{bay_number}/start")
def start_session(bay_number: int, request: SessionRequest) -> dict:
    return controller.start_session(bay_number, request.seconds, request.wash_mode)


@app.post("/api/bays/{bay_number}/stop")
def stop_session(bay_number: int) -> dict:
    return controller.stop_session(bay_number)


@app.get("/api/hardware/ports")
def hardware_ports() -> list[dict]:
    return list_serial_ports()


@app.post("/api/hardware/connect")
def connect_hardware(request: ConnectRequest) -> dict:
    return controller.connect_hardware(request.port)


@app.post("/api/hardware/disconnect")
def disconnect_hardware() -> dict:
    return controller.disconnect_hardware()


@app.post("/api/hardware/ping")
def ping_hardware() -> dict:
    return controller.ping_hardware()


@app.post("/api/pricing/preview")
def pricing_preview(request: PreviewRequest) -> dict:
    return controller.preview(request.bay_number, request.seconds)


@app.post("/api/simulator/scenario")
def simulator_scenario(request: ScenarioRequest) -> dict:
    return controller.trigger_scenario(request.bay_number, request.scenario)


@app.get("/api/analytics")
def analytics() -> dict:
    return controller.analytics()


@app.get("/api/faults")
def faults() -> list[dict]:
    return controller.database.recent_faults(100)


@app.post("/api/faults/{fault_id}/resolve")
def resolve_fault(fault_id: int) -> dict:
    return controller.resolve_fault(fault_id)
