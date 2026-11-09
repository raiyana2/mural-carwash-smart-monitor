"""hardware_bay.py: Bay 1, the bay wired to the real ESP32.

HardwareBay owns the one USB serial link: connecting (by hand or automatically),
polling the motor and rain sensor, running the motor, and noticing when the link
drops. It writes what it sees into the database for the dashboard to read.

A run turns the servo from MOTOR_REST_ANGLE (0°) to MOTOR_RUN_ANGLE (180°), holds it,
and the ESP32 turns it back on its own timer, even if the Pi or the cable drops.

The rain sensor stands in for a water flow meter: while the valve is open its readings
are collected, a wash with no water raises an alert FLOW_CHECK_SECONDS in, and each
wash's average flow is saved for the repair predictions (maintenance.py).

With firmware that has them (it lists its features when pinged), HardwareBay also:
  * reads the ultrasonic sensor every DISTANCE_POLL_SECONDS: it shows whether a car is
    in the bay, and counts each car that pulls in
  * hands each press of the bay's button to on_button (MonitorController starts a wash)
  * keeps the bay's LCD showing the price and longest wash, or that the bay is out of
    service; during a wash the firmware shows the price paid and counts down the time

Threads: MonitorController's worker calls maybe_auto_connect() without the shared
lock (opening a port can take seconds) and poll_locked() with it. Methods ending in
_locked expect the caller to hold the lock.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

import serial

from carwash.config import (
    BUTTON_RUN_SECONDS,
    CAR_CONFIRM_POLLS,
    DISTANCE_POLL_SECONDS,
    DISTANCE_STALE_SECONDS,
    ESP32_PORT,
    FLOW_CHECK_SECONDS,
    FLOW_FULL_SCALE_LPM,
    FLOW_NONE_LPM,
    FLOW_SETTLE_SECONDS,
    HARDWARE_BAY,
    HARDWARE_STALE_SECONDS,
    MOTOR_REST_ANGLE,
    MOTOR_RUN_ANGLE,
    RAIN_POLL_SECONDS,
    RAIN_STALE_SECONDS,
    RETRY_SECONDS,
    RETRY_SECONDS_DEFAULT,
    VEHICLE_PRESENT_CM,
)
from carwash.controllers.errors import ControllerError
from carwash.controllers.pricing import price_for_seconds
from carwash.hardware.esp32_link import Esp32ConnectError, Esp32Error, Esp32Link, MotorStatus, list_serial_ports
from carwash.storage.database import MonitorDatabase

if TYPE_CHECKING:
    from carwash.controllers.maintenance import MaintenanceAnalyzer

log = logging.getLogger(__name__)

# Errors that mean the serial link itself is broken (as opposed to Esp32Error,
# where the ESP32 answered and said no).
LINK_ERRORS = (TimeoutError, serial.SerialException, OSError, RuntimeError)


class HardwareBay:
    def __init__(
        self,
        database: MonitorDatabase,
        lock: threading.RLock,
        serial_port: Optional[str] = None,
        auto_connect: bool = True,
        maintenance: Optional["MaintenanceAnalyzer"] = None,
    ) -> None:
        self.database = database
        self.maintenance = maintenance
        self.bay_number = HARDWARE_BAY
        self.serial_port = serial_port
        self._lock = lock                     # shared with MonitorController
        self._open_lock = threading.Lock()    # only one attempt to open the port at a time
        self._link: Optional[Esp32Link] = None
        self._link_source = "hardware"
        self._ever_connected = False
        self._auto_connect = auto_connect     # paused by disconnect(), resumed by connect()
        self._next_connect_at = 0.0
        self._next_rain_at = 0.0
        self._last_error: Optional[str] = None
        self._last_error_kind: Optional[str] = None
        self._servo_angle: Optional[int] = None
        self._esp_restarted = False           # set by the serial reader thread on a "ready" event
        self._next_distance_at = 0.0
        self._lcd_shown: Optional[tuple[str, str]] = None   # the LCD text last sent
        # Car counter: a car counts once it has been in range for CAR_CONFIRM_POLLS
        # readings, and the next car can count once it's been gone that long.
        self._car_seen = 0
        self._car_gone = 0
        self._car_counted = False
        # The wash in progress (valve open): when it started, its flow readings, and
        # whether the no-water alert has been raised.
        self._wash: Optional[dict] = None
        # Button presses: the reader thread sets _button_pending and calls wake();
        # poll_locked() then calls on_button() under the lock.
        self.on_button: Optional[Callable[[], None]] = None   # set by MonitorController
        self.wake: Optional[Callable[[], None]] = None        # set by MonitorController
        self._button_pending = False
        self.last_button_at: Optional[float] = None
        self.last_button_result: Optional[str] = None

    # ------------------------------------------------------------------
    # connecting
    # ------------------------------------------------------------------
    @staticmethod
    def serial_ports() -> list[dict]:
        return list_serial_ports()

    def _port_to_use(self) -> Optional[str]:
        return self.serial_port or ESP32_PORT

    def _on_esp_event(self, event: dict) -> None:
        # Runs on the serial reader thread, which must not send requests itself (it's the
        # thread that delivers their replies). poll_locked() handles these under the lock.
        if event.get("event") == "ready":
            self._esp_restarted = True
            self._lcd_shown = None            # a restarted board has forgotten the LCD text
        elif event.get("event") == "button" and event.get("pressed", True):
            self.last_button_at = time.time()
            self._button_pending = True
            if self.wake:
                self.wake()                   # handle it now, not at the next poll

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
        self._last_error = message
        self._last_error_kind = kind
        self._next_connect_at = time.time() + RETRY_SECONDS.get(kind, RETRY_SECONDS_DEFAULT)

    def maybe_auto_connect(self) -> None:
        """Try to connect if it's time to. Call WITHOUT holding the lock."""
        if self._link is not None or not self._auto_connect or time.time() < self._next_connect_at:
            return
        if not self._open_lock.acquire(blocking=False):
            return  # a manual connect is already opening the port
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

    def _install_link_locked(self, link) -> None:
        self._link = link
        self._link_source = "simulator" if link.port and str(link.port).startswith("/dev/pts/") else "hardware"
        self._ever_connected = True
        self._last_error = None
        self._last_error_kind = None
        self._esp_restarted = False   # a reset while opening isn't news
        self._next_rain_at = 0.0
        self._next_distance_at = 0.0
        self._lcd_shown = None
        self._button_pending = False
        self._car_seen = self._car_gone = 0
        self._car_counted = False
        self._wash = None
        self._servo_angle = link.angle
        self.database.resolve_faults(self.bay_number, "serial_connection_lost")
        self.database.update_bay(
            self.bay_number,
            {"vehicle_sensor_installed": int(link.has("distance")), "vehicle_present": None,
             "distance_cm": None, "distance_updated_at": None},
        )

    def connect(self, port: Optional[str]) -> None:
        """Connect to `port` (blank = auto-detect) and verify it. Raises ControllerError."""
        with self._open_lock:
            with self._lock:
                self._close_link_locked()
                self.serial_port = (port or "").strip() or None
                self._auto_connect = True
            try:
                link = self._open_link()
            except Esp32ConnectError as exc:
                raise ControllerError(str(exc), "unavailable") from exc
            with self._lock:
                self._install_link_locked(link)
                self.poll_locked()
                if self._link is None:
                    raise ControllerError(
                        f"The ESP32 on {link.port} answered once, then stopped: {self._last_error or 'no reply'}",
                        "unavailable",
                    )

    def disconnect(self) -> None:
        """Let go of the port and stay off it until connect(), so a PlatformIO upload
        or serial monitor can use it."""
        with self._lock:
            self._auto_connect = False
            self._show_offline_locked()
            self._close_link_locked()
            self._mark_offline_locked()

    def ping(self) -> dict:
        with self._lock:
            link = self._require_link_locked()
            try:
                uptime = link.ping()
                motor = link.motor_status()
            except Esp32Error as exc:
                raise ControllerError(f"The ESP32 refused: {exc}", "conflict") from exc
            except LINK_ERRORS as exc:
                self._connection_lost_locked(exc)
                raise ControllerError(f"ESP32 ping failed: {exc}", "unavailable") from exc
            self._record_motor_locked(motor)
            return {"uptime_s": uptime, "connected": True}

    def close(self) -> None:
        with self._lock:
            self._show_offline_locked()
            self._close_link_locked()

    def _require_link_locked(self) -> Esp32Link:
        if self._link is None or not self.status()["connected"]:
            raise ControllerError(
                "Bay 1's ESP32 isn't connected. Connect it on the Maintenance page.", "unavailable"
            )
        return self._link

    # ------------------------------------------------------------------
    # polling
    # ------------------------------------------------------------------
    def poll_locked(self) -> None:
        """Read the motor state (and the rain sensor every few seconds) into the database."""
        link = self._link
        if link is None:
            return
        if not link.connected:
            self._connection_lost_locked(RuntimeError("the USB serial port closed (cable unplugged?)"))
            return
        distance_read = False
        distance = None
        try:
            motor = link.motor_status()
            reading = None
            if time.time() >= self._next_rain_at:
                reading = link.read_rain()
                self._next_rain_at = time.time() + RAIN_POLL_SECONDS
            if link.has("distance") and time.time() >= self._next_distance_at:
                distance = link.read_distance()
                distance_read = True
                self._next_distance_at = time.time() + DISTANCE_POLL_SECONDS
        except Esp32Error as exc:
            log.warning("ESP32 refused a status request: %s", exc)  # it answered: the link is fine
            return
        except LINK_ERRORS as exc:
            self._connection_lost_locked(exc)
            return
        if distance_read:
            self._count_car_locked(distance)
            self.database.update_bay(
                self.bay_number,
                {
                    # No echo means nothing within range, so no car.
                    "vehicle_present": int(distance is not None and distance < VEHICLE_PRESENT_CM),
                    "distance_cm": distance,
                    "distance_updated_at": time.time(),
                },
            )
        if reading is not None:
            self.database.update_bay(
                self.bay_number,
                {
                    "rain_detected": int(reading.raining),
                    "rain_raw": reading.raw,
                    "rain_wet_percent": reading.wet_percent,
                    "rain_updated_at": time.time(),
                },
            )
        self._record_motor_locked(motor)
        if reading is not None:
            self._flow_sample_locked(reading.wet_percent / 100 * FLOW_FULL_SCALE_LPM)
        if self._button_pending:
            self._button_pending = False
            if self.on_button:
                self.on_button()
        self._sync_lcd_locked()

    # ------------------------------------------------------------------
    # car counter and water flow
    # ------------------------------------------------------------------
    def _count_car_locked(self, distance: Optional[float]) -> None:
        if distance is not None and distance < VEHICLE_PRESENT_CM:
            self._car_gone = 0
            self._car_seen += 1
            if self._car_seen >= CAR_CONFIRM_POLLS and not self._car_counted:
                self._car_counted = True
                self.database.add_reading(self.bay_number, "car", 1, self._link_source)
        else:
            self._car_seen = 0
            self._car_gone += 1
            if self._car_gone >= CAR_CONFIRM_POLLS:
                self._car_counted = False

    def _flow_sample_locked(self, flow_lpm: float) -> None:
        wash = self._wash
        if wash is None:
            return
        elapsed = time.time() - wash["started"]
        if elapsed < FLOW_SETTLE_SECONDS:
            return
        wash["samples"].append(flow_lpm)
        if (not wash["alerted"] and elapsed >= FLOW_CHECK_SECONDS and self.maintenance
                and max(wash["samples"]) < FLOW_NONE_LPM):
            wash["alerted"] = True
            self.maintenance.no_flow_alert(
                self.bay_number, self._link_source, f"{flow_lpm:.1f} L/min {elapsed:.0f} s into the wash")

    def _finish_wash_locked(self) -> None:
        wash, self._wash = self._wash, None
        if not wash or not wash["samples"]:
            return  # too short to measure, or no flow meter readings
        average = sum(wash["samples"]) / len(wash["samples"])
        self.database.add_reading(self.bay_number, "flow", round(average, 2), self._link_source)
        if self.maintenance:
            self.maintenance.evaluate_after_run(self.bay_number, self._link_source)

    # ------------------------------------------------------------------
    # LCD
    # ------------------------------------------------------------------
    def _lcd_lines(self, bay: dict) -> tuple[str, str]:
        """What the bay's LCD should say between washes (16 characters per line)."""
        if not bay["enabled"]:
            return ("Out of service", "")
        if bay["status"] == "faulted":
            return ("See attendant", "")
        base, included = bay["base_price_cents"], bay["included_seconds"]
        per_minute = bay["additional_price_per_minute_cents"]
        if base == per_minute and included == 60:
            price = f"${per_minute / 100:.2f} per minute"
            if len(price) > 16:
                price = f"${per_minute / 100:.2f}/minute"
        else:
            price = f"${base / 100:.2f} for {max(1, included // 60)} min"
        return (price, f"Max {bay['maximum_duration_seconds'] // 60} min")

    @staticmethod
    def wash_label(price_cents: int) -> str:
        """LCD line 1 during a wash (line 2 is the firmware's countdown)."""
        return f"Wash ${price_cents / 100:.2f}"

    def _sync_lcd_locked(self) -> None:
        """Send the LCD text when it should change (not on every poll)."""
        link = self._link
        if link is None or not link.has("lcd"):
            return
        lines = self._lcd_lines(self.database.get_bay(self.bay_number))
        if lines == self._lcd_shown:
            return
        try:
            link.lcd_text(0, lines[0])
            link.lcd_text(1, lines[1])
        except Esp32Error as exc:
            log.warning("ESP32 refused the LCD text: %s", exc)
        except LINK_ERRORS as exc:
            self._connection_lost_locked(exc)
            return
        self._lcd_shown = lines

    def _show_offline_locked(self) -> None:
        """Before letting go of the board, tell people at the bay it isn't being controlled."""
        link = self._link
        if link is None or not link.has("lcd") or not link.connected:
            return
        try:
            link.lcd_text(0, f"Bay {self.bay_number} offline")
            link.lcd_text(1, "")
        except (Esp32Error, *LINK_ERRORS):
            pass

    def _record_motor_locked(self, motor: MotorStatus) -> None:
        """Store the ESP32's motor state and end a run the ESP32 has finished."""
        now = time.time()
        self._servo_angle = motor.angle
        bay = self.database.get_bay(self.bay_number)
        active = next((s for s in bay["sessions"] if s["status"] == "active"), None)
        if self._esp_restarted:
            self._esp_restarted = False
            self.database.add_fault(
                self.bay_number,
                "hardware",
                "esp32_restarted",
                "warning",
                "The ESP32 restarted on its own"
                + (", ending the motor run early" if active else "")
                + ". A common cause is powering the servo from the board's USB 5 V (it browns out "
                "when the servo moves); give the servo its own 5 V supply with a shared ground.",
                active["id"] if active else None,
            )
        if motor.running and self._wash is None:
            self._wash = {"started": now, "samples": [], "alerted": False}
        elif not motor.running and self._wash is not None:
            self._finish_wash_locked()
        if active and not motor.running:
            self.database.end_session(self.bay_number)
        critical = any(f["active"] and f["severity"] == "critical" for f in bay["faults"])
        self.database.update_bay(
            self.bay_number,
            {
                "last_seen_at": now,
                "connection_source": self._link_source,
                "motor_running": int(motor.running),
                "motor_remaining_s": motor.remaining_s,
                "motor_updated_at": now,
                "status": "occupied" if motor.running else ("faulted" if critical else "available"),
            },
        )
        self._last_error = None
        self._last_error_kind = None

    def _connection_lost_locked(self, error: Exception) -> None:
        port = self._link.port if self._link else self._port_to_use()
        self._close_link_locked()
        self._mark_offline_locked()
        self._last_error = f"Lost the ESP32 on {port}: {error}"
        self._last_error_kind = "lost"
        self._next_connect_at = time.time() + RETRY_SECONDS["lost"]
        if self._ever_connected:
            self.database.add_fault(
                self.bay_number,
                "hardware",
                "serial_connection_lost",
                "warning",
                f"ESP32 serial communication stopped responding: {error}. Sensor values are unavailable until reconnection.",
            )

    def _mark_offline_locked(self) -> None:
        self.database.update_bay(
            self.bay_number,
            {"status": "offline", "motor_running": None, "motor_remaining_s": None,
             "motor_updated_at": None, "last_seen_at": None},
        )

    def _close_link_locked(self) -> None:
        self._servo_angle = None
        self._wash = None
        if self._link is not None:
            self._link.close()
            self._link = None

    # ------------------------------------------------------------------
    # motor runs (MonitorController checks the bay is allowed to run first)
    # ------------------------------------------------------------------
    def start_run_locked(self, bay: dict, seconds: int, wash_mode: str) -> int:
        """Turn to 180°, hold `seconds`, then the ESP32 turns back to 0°. Returns the run's id."""
        link = self._require_link_locked()
        try:
            status = link.motor_status()
            if status.running:
                raise ControllerError("Bay 1 is already washing. Press Stop wash first.", "conflict")
            if status.angle is not None and status.angle != MOTOR_REST_ANGLE:
                # Start from rest so every run is the full 0° -> 180° turn and the
                # ESP32 turns it back to 0° when the timer ends.
                try:
                    link.servo_set(MOTOR_REST_ANGLE)
                except Esp32Error:
                    pass  # older firmware without servo_set returns to rest by itself
            motor = link.motor_on(seconds, angle=MOTOR_RUN_ANGLE,
                                  label=self.wash_label(price_for_seconds(bay, seconds)))
        except ControllerError:
            raise
        except Esp32Error as exc:
            raise ControllerError(f"The ESP32 refused the run: {exc}", "conflict") from exc
        except LINK_ERRORS as exc:
            self._connection_lost_locked(exc)
            raise ControllerError(f"Bay 1 motor command failed: {exc}", "unavailable") from exc
        session_id = self.database.start_session(
            self.bay_number, self._link_source, wash_mode, seconds, seconds, price_for_seconds(bay, seconds)
        )
        self._record_motor_locked(motor)
        return session_id

    def stop_run_locked(self) -> None:
        """Turn back to 0° now. Works whenever the motor is running, even with no recorded run."""
        link = self._require_link_locked()
        try:
            motor = link.motor_off()
        except Esp32Error as exc:
            raise ControllerError(f"The ESP32 refused to stop: {exc}", "conflict") from exc
        except LINK_ERRORS as exc:
            self._connection_lost_locked(exc)
            raise ControllerError(f"Bay 1 motor stop failed: {exc}", "unavailable") from exc
        self.database.end_session(self.bay_number, "completed")
        self._record_motor_locked(motor)

    # ------------------------------------------------------------------
    # reading state
    # ------------------------------------------------------------------
    def status(self) -> dict:
        bay = self.database.get_bay(self.bay_number)
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
            "features": sorted(self._link.features) if connected and self._link else [],
            "firmware_version": self._link.firmware_version if connected and self._link else None,
            "button_run_seconds": BUTTON_RUN_SECONDS,
            "last_button_at": self.last_button_at,
            "last_button_result": self.last_button_result,
            "port": self._link.port if connected and self._link else self._port_to_use(),
            "last_error": None if connected else self._last_error,
            "last_error_kind": None if connected else self._last_error_kind,
            "last_seen_at": bay["last_seen_at"] if bay else None,
            "auto_connect": self._auto_connect,
            "retry_in_s": retry_in,
        }

    def decorate(self, bay: dict, status: dict, now: float) -> dict:
        """Add live fields to Bay 1's row; readings that are stale or from a dropped link become None."""
        bay["connected"] = status["connected"]
        bay["connected_port"] = status["port"]
        bay["features"] = status["features"]
        if (not bay.get("distance_updated_at") or now - bay["distance_updated_at"] > DISTANCE_STALE_SECONDS
                or not status["connected"]):
            bay["vehicle_present"] = None
            bay["distance_cm"] = None
        if not bay["rain_updated_at"] or now - bay["rain_updated_at"] > RAIN_STALE_SECONDS or not status["connected"]:
            bay["rain_detected"] = None
            bay["rain_raw"] = None
            bay["rain_wet_percent"] = None
        if not bay["motor_updated_at"] or now - bay["motor_updated_at"] > HARDWARE_STALE_SECONDS or not status["connected"]:
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
        # The rain sensor read as a flow meter (live, so None when stale).
        bay["flow_lpm"] = (None if bay["rain_wet_percent"] is None
                           else round(bay["rain_wet_percent"] / 100 * FLOW_FULL_SCALE_LPM, 1))
        return bay
