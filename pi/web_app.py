"""Local multi-bay dashboard and the single owner of the Bay 1 serial link."""

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
from esp32_link import Esp32Error, Esp32Link

STATIC_DIR = Path(__file__).parent / "static"
MAX_MOTOR_SECONDS = 300
HARDWARE_STALE_SECONDS = 10
RAIN_STALE_SECONDS = 15
WASH_MODES = ("Standard", "Rinse", "Foam", "Wax")
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
    ) -> None:
        self.database = database or MonitorDatabase()
        self.serial_port = serial_port
        self.run_simulator = run_simulator
        self.seed_demo_data = seed_demo_data
        self._link: Optional[Esp32Link] = None
        self._link_source = "hardware"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._ever_connected = False
        self._next_connect_at = 0.0
        self._next_rain_at = 0.0
        self._next_sim_at = 0.0
        self._last_connection_error: Optional[str] = None
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

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    self._poll_hardware_locked()
                    if self.run_simulator and time.time() >= self._next_sim_at:
                        self._tick_simulator_locked()
                        self._next_sim_at = time.time() + 2
            except Exception:
                log.exception("Bay monitor iteration failed")
            self._stop.wait(1)

    def _poll_hardware_locked(self) -> None:
        if self._link is None:
            if time.time() < self._next_connect_at:
                return
            self._next_connect_at = time.time() + 3
            link = Esp32Link(port=self.serial_port or os.getenv("ESP32_PORT") or None)
            try:
                link.open()
                self._link = link
                self._link_source = self._source_for_port(link.port)
                self._ever_connected = True
                self._last_connection_error = None
                self.database.resolve_faults(1, "serial_connection_lost")
            except (TimeoutError, serial.SerialException, OSError, RuntimeError) as exc:
                link.close()
                self._last_connection_error = str(exc)
                return

        link = self._link
        if link is None:
            return
        try:
            uptime = link.ping()
            motor = link.motor_status()
            now = time.time()
            updates = {
                "last_seen_at": now,
                "connection_source": self._link_source,
                "motor_running": int(motor.running),
                "motor_remaining_s": motor.remaining_s,
                "motor_updated_at": now,
                "status": "occupied" if motor.running else "available",
            }
            if now >= self._next_rain_at:
                reading = link.read_rain()
                updates.update(
                    rain_detected=int(reading.raining),
                    rain_raw=reading.raw,
                    rain_wet_percent=reading.wet_percent,
                    rain_updated_at=now,
                )
                self._next_rain_at = now + 5
            self.database.update_bay(1, updates)
            if not motor.running:
                active_session = next(
                    (session for session in self.database.get_bay(1)["sessions"] if session["status"] == "active"),
                    None,
                )
                if active_session:
                    self.database.end_session(1)
            self._last_connection_error = None
        except (Esp32Error, TimeoutError, serial.SerialException, OSError, RuntimeError) as exc:
            self._last_connection_error = str(exc)
            self._connection_lost_locked(exc)

    def _connection_lost_locked(self, error: Exception) -> None:
        was_connected = self._ever_connected
        self._close_link_locked()
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
        if was_connected:
            self.database.add_fault(
                1,
                "hardware",
                "serial_connection_lost",
                "warning",
                f"ESP32 serial communication stopped responding: {error}. Sensor values are unavailable until reconnection.",
            )

    def _close_link_locked(self) -> None:
        if self._link is not None:
            self._link.close()
            self._link = None

    @staticmethod
    def _source_for_port(port: Optional[str]) -> str:
        return "simulator" if port and port.startswith("/dev/pts/") else "hardware"

    def connect_hardware(self, port: Optional[str]) -> dict:
        with self._lock:
            self._close_link_locked()
            self.serial_port = port or None
            self._next_connect_at = 0
            link = Esp32Link(port=self.serial_port or os.getenv("ESP32_PORT") or None)
            try:
                link.open()
                self._link = link
                self._link_source = self._source_for_port(link.port)
                self._ever_connected = True
                self._last_connection_error = None
                self.database.resolve_faults(1, "serial_connection_lost")
                self._poll_hardware_locked()
                if self._link is None:
                    raise HTTPException(
                        status_code=503,
                        detail=f"ESP32 did not stay connected: {self._last_connection_error or 'no reply'}",
                    )
                return self.bay_detail(1)
            except (TimeoutError, serial.SerialException, OSError, RuntimeError) as exc:
                link.close()
                self._link = None
                self._last_connection_error = str(exc)
                raise HTTPException(status_code=503, detail=f"Could not connect to Bay 1 ESP32: {exc}") from exc

    def disconnect_hardware(self) -> dict:
        with self._lock:
            self._close_link_locked()
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
            return {"connected": False}

    def ping_hardware(self) -> dict:
        with self._lock:
            link = self._require_link_locked()
            try:
                uptime = link.ping()
                motor = link.motor_status()
                self.database.update_bay(
                    1,
                    {
                        "last_seen_at": time.time(),
                        "connection_source": self._link_source,
                        "motor_running": int(motor.running),
                        "motor_remaining_s": motor.remaining_s,
                        "motor_updated_at": time.time(),
                        "status": "occupied" if motor.running else "available",
                    },
                )
                return {"uptime_s": uptime, "connected": True}
            except (Esp32Error, TimeoutError, serial.SerialException, OSError, RuntimeError) as exc:
                self._connection_lost_locked(exc)
                raise HTTPException(status_code=503, detail=f"ESP32 ping failed: {exc}") from exc

    def dashboard(self) -> dict:
        result = self.database.dashboard()
        hardware = self._hardware_status()
        result["hardware"] = hardware
        physical = result["bays"][0]
        physical["connected"] = hardware["connected"]
        physical["connected_port"] = hardware["port"]
        if (
            not physical["rain_updated_at"]
            or time.time() - physical["rain_updated_at"] > RAIN_STALE_SECONDS
            or not hardware["connected"]
        ):
            physical["rain_detected"] = None
            physical["rain_raw"] = None
            physical["rain_wet_percent"] = None
        if (
            not physical["motor_updated_at"]
            or time.time() - physical["motor_updated_at"] > HARDWARE_STALE_SECONDS
            or not hardware["connected"]
        ):
            physical["motor_running"] = None
            physical["motor_remaining_s"] = None
        return result

    def _hardware_status(self) -> dict:
        bay = self.database.get_bay(1)
        connected = bool(
            self._link
            and bay
            and bay["last_seen_at"]
            and time.time() - bay["last_seen_at"] <= HARDWARE_STALE_SECONDS
        )
        return {
            "connected": connected,
            "port": self._link.port if connected and self._link else self.serial_port,
            "last_error": self._last_connection_error,
            "last_seen_at": bay["last_seen_at"] if bay else None,
        }

    def bay_detail(self, bay_number: int) -> dict:
        bay = self.database.get_bay(bay_number)
        if bay is None:
            raise HTTPException(status_code=404, detail="Bay not found")
        now = time.time()
        bay["connected"] = bay_number == 1 and self._hardware_status()["connected"]
        bay["connected_port"] = self._hardware_status()["port"] if bay_number == 1 else None
        if bay_number == 1:
            if not bay["rain_updated_at"] or now - bay["rain_updated_at"] > RAIN_STALE_SECONDS:
                bay["rain_detected"] = None
                bay["rain_raw"] = None
                bay["rain_wet_percent"] = None
            if not bay["motor_updated_at"] or now - bay["motor_updated_at"] > HARDWARE_STALE_SECONDS:
                bay["motor_running"] = None
                bay["motor_remaining_s"] = None
        bay["remaining_seconds"] = None
        active = next((session for session in bay["sessions"] if session["status"] == "active"), None)
        if active:
            elapsed = max(0, int(now - active["started_at"]))
            bay["remaining_seconds"] = max(0, active["granted_seconds"] - elapsed)
        return bay

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

    def start_session(self, bay_number: int, seconds: int, mode: Optional[str]) -> dict:
        with self._lock:
            return self._start_session(bay_number, seconds, mode)

    def _start_session(self, bay_number: int, seconds: int, mode: Optional[str]) -> dict:
        bay = self.database.get_bay(bay_number)
        if bay is None:
            raise HTTPException(status_code=404, detail="Bay not found")
        if not bay["enabled"]:
            raise HTTPException(status_code=409, detail="This bay is disabled")
        if bay["status"] == "faulted":
            raise HTTPException(status_code=409, detail="Resolve the active bay fault before starting a wash")
        if any(session["status"] == "active" for session in bay["sessions"]):
            raise HTTPException(status_code=409, detail=f"{bay['label']} already has an active session")
        if seconds > bay["maximum_duration_seconds"] or seconds > MAX_MOTOR_SECONDS:
            raise HTTPException(
                status_code=422,
                detail=f"Wash duration exceeds the bay or ESP32 maximum ({min(bay['maximum_duration_seconds'], MAX_MOTOR_SECONDS)} seconds)",
            )
        if mode and mode not in WASH_MODES:
            raise HTTPException(status_code=422, detail="Unsupported wash mode")
        wash_mode = mode or bay["default_wash_mode"]
        if bay["source"] == "hardware":
            with self._lock:
                link = self._require_link_locked()
                try:
                    if link.motor_status().running:
                        raise HTTPException(status_code=409, detail="Bay 1 motor is already running")
                    motor = link.motor_on(seconds)
                    session_id = self.database.start_session(
                        bay_number,
                        self._link_source,
                        wash_mode,
                        seconds,
                        seconds,
                        price_for_seconds(bay, seconds),
                    )
                    self.database.update_bay(
                        1,
                        {
                            "status": "occupied",
                            "motor_running": int(motor.running),
                            "motor_remaining_s": motor.remaining_s,
                            "motor_updated_at": time.time(),
                        },
                    )
                    return {"session_id": session_id, **self.bay_detail(1)}
                except HTTPException:
                    raise
                except (Esp32Error, TimeoutError, serial.SerialException, OSError, RuntimeError) as exc:
                    self._connection_lost_locked(exc)
                    raise HTTPException(status_code=503, detail=f"Bay 1 motor command failed: {exc}") from exc
        try:
            session_id = self.database.start_session(
                bay_number,
                "simulator",
                wash_mode,
                seconds,
                seconds,
                price_for_seconds(bay, seconds),
            )
            self.database.update_bay(
                bay_number,
                {
                    "motor_running": 1,
                    "motor_remaining_s": float(seconds),
                    "motor_updated_at": time.time(),
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"session_id": session_id, **self.bay_detail(bay_number)}

    def stop_session(self, bay_number: int) -> dict:
        with self._lock:
            return self._stop_session(bay_number)

    def _stop_session(self, bay_number: int) -> dict:
        bay = self.database.get_bay(bay_number)
        if bay is None:
            raise HTTPException(status_code=404, detail="Bay not found")
        if bay["source"] == "hardware":
            with self._lock:
                link = self._require_link_locked()
                try:
                    motor = link.motor_off()
                    self.database.end_session(bay_number, "completed")
                    self.database.update_bay(
                        1,
                        {
                            "status": "available",
                            "motor_running": int(motor.running),
                            "motor_remaining_s": motor.remaining_s,
                            "motor_updated_at": time.time(),
                        },
                    )
                except (Esp32Error, TimeoutError, serial.SerialException, OSError, RuntimeError) as exc:
                    self._connection_lost_locked(exc)
                    raise HTTPException(status_code=503, detail=f"Bay 1 motor stop failed: {exc}") from exc
        else:
            self.database.end_session(bay_number, "completed")
        return self.bay_detail(bay_number)

    def _require_link_locked(self) -> Esp32Link:
        if self._link is None or not self._hardware_status()["connected"]:
            raise HTTPException(status_code=503, detail="Bay 1 ESP32 is disconnected or its status is stale")
        return self._link

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
            if bay["status"] != "occupied":
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
                raise HTTPException(status_code=409, detail="End the current session before injecting an overrun")
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


@app.post("/api/bays/{bay_number}/start")
def start_session(bay_number: int, request: SessionRequest) -> dict:
    return controller.start_session(bay_number, request.seconds, request.wash_mode)


@app.post("/api/bays/{bay_number}/stop")
def stop_session(bay_number: int) -> dict:
    return controller.stop_session(bay_number)


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
