"""esp32_link.py: talk to the ESP32 motor/rain controller over USB serial.

    from esp32_link import Esp32Link

    with Esp32Link() as esp:            # finds the ESP32's USB port automatically
        esp.servo_set(90)                # move to 90° and stay there
        esp.motor_on(5)                  # timed hold: servo to its "on" angle for 5 s, then back to where it was
        esp.motor_on(5, angle=90)        # ...or to a specific angle (after the first one ends)
        print(esp.motor_status())        # MotorStatus(running=True, remaining_s=4.9, angle=180)
        esp.wait_until_motor_stops()
        print(esp.read_rain())           # RainReading(raw=1830, wet_percent=55.3, raining=True)

How it works: every request gets a number ("id"). A background thread reads
lines from the ESP32 and hands each reply to whoever is waiting for that id.
Lines that aren't replies are "events" (like the motor stopping on its own),
which update the motor state here and are passed to your on_event callback.
"""
from __future__ import annotations

import itertools
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import serial
from serial.tools import list_ports

log = logging.getLogger(__name__)

# USB vendor ids of the USB-serial chips found on ESP32 boards:
# Silicon Labs CP210x, WCH CH340/CH9102, FTDI, Espressif native USB
ESP32_USB_VIDS = {0x10C4, 0x1A86, 0x0403, 0x303A}


class Esp32Error(Exception):
    """The ESP32 answered but refused the request (ok=false)."""


@dataclass
class MotorStatus:
    running: bool        # True while the servo is holding its "on" angle
    remaining_s: float   # seconds left before it returns to its original angle
    angle: int | None = None  # current servo angle (0-180)


@dataclass
class RainReading:
    raw: int            # 0..4095 from the ADC; lower = wetter
    wet_percent: float  # 0 = dry, 100 = soaked
    raining: bool       # from the module's digital output (threshold set by its knob)


def find_esp32_port() -> str:
    ports = [p.device for p in list_ports.comports() if p.vid in ESP32_USB_VIDS]
    if not ports:
        raise RuntimeError("no ESP32 found on USB; pass the port explicitly (e.g. COM5 or /dev/ttyUSB0)")
    if len(ports) > 1:
        log.warning("several ESP32-like ports found %s; using %s", ports, ports[0])
    return ports[0]


class Esp32Link:
    def __init__(
        self,
        port: str | None = None,
        baud: int = 115200,
        reply_timeout: float = 2.0,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.port = port
        self.baud = baud
        self.reply_timeout = reply_timeout
        self.on_event = on_event

        self._ser: serial.Serial | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()

        self._ids = itertools.count(1)
        self._pending: dict[int, dict] = {}   # id -> {"done": Event, "reply": dict | None}
        self._pending_lock = threading.Lock()

        self._motor_running = False
        self._motor_stopped = threading.Event()
        self._motor_stopped.set()
        self._angle: int | None = None

    # ------------------------------------------------------------------
    # connect / disconnect
    # ------------------------------------------------------------------
    def open(self) -> "Esp32Link":
        s = serial.Serial()
        s.port = self.port or find_esp32_port()
        s.baudrate = self.baud
        s.timeout = 0.1
        # Most ESP32 boards reset when DTR/RTS change. Keeping both off stops
        # the board (and a running motor) from resetting just because we connected.
        s.dtr = False
        s.rts = False
        s.open()
        self._ser = s
        self.port = s.port

        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="esp32-reader", daemon=True)
        self._reader.start()

        # The board may still have reset; keep pinging until it answers.
        deadline = time.monotonic() + 5
        while True:
            try:
                self.ping(timeout=0.5)
                break
            except TimeoutError:
                if time.monotonic() > deadline:
                    self.close()
                    raise TimeoutError(f"ESP32 on {self.port} is not answering (is the firmware flashed?)")
        self.motor_status()  # learn the current motor state
        log.info("connected to ESP32 on %s", self.port)
        return self

    def close(self) -> None:
        self._stop.set()
        if self._reader:
            self._reader.join(timeout=1)
        if self._ser:
            self._ser.close()
        self._ser = None

    def __enter__(self) -> "Esp32Link":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def ping(self, timeout: float | None = None) -> float:
        """Returns the ESP32's uptime in seconds."""
        return self._request("ping", timeout=timeout)["uptime_s"]

    def motor_on(self, seconds: float, angle: int | None = None) -> MotorStatus:
        """Move the servo to `angle` (default: the firmware's SERVO_ACTIVE_ANGLE),
        hold it for `seconds`, then the ESP32 returns it to the angle it was at before, by itself."""
        if seconds <= 0:
            raise ValueError("seconds must be > 0")
        if angle is not None and not 0 <= angle <= 180:
            raise ValueError("angle must be 0-180")
        fields = {"seconds": seconds}
        if angle is not None:
            fields["angle"] = int(angle)
        return self._motor_reply(self._request("motor_on", **fields))

    def motor_off(self) -> MotorStatus:
        return self._motor_reply(self._request("motor_off"))

    def motor_status(self) -> MotorStatus:
        return self._motor_reply(self._request("motor_status"))

    def servo_set(self, angle: int) -> MotorStatus:
        """Move the servo to `angle` (0-180) and leave it there (no timer).
        The ESP32 refuses this while a timed hold (motor_on) is running."""
        if not 0 <= angle <= 180:
            raise ValueError("angle must be 0-180")
        return self._motor_reply(self._request("servo_set", angle=int(angle)))

    def servo_status(self) -> MotorStatus:
        """Same information as motor_status(): angle, and whether a timed hold is running."""
        return self._motor_reply(self._request("servo_status"))

    @property
    def angle(self) -> int | None:
        """Last known servo angle (kept current by replies and events, no request sent)."""
        return self._angle

    def read_rain(self) -> RainReading:
        d = self._request("rain")
        return RainReading(raw=d["raw"], wet_percent=d["wet_percent"], raining=d["raining"])

    @property
    def connected(self) -> bool:
        """True while the port is open and the background reader is still running."""
        return bool(self._ser and self._ser.is_open and self._reader and self._reader.is_alive())

    @property
    def motor_running(self) -> bool:
        """Last known motor state (kept current by replies and events, no request sent)."""
        return self._motor_running

    def wait_until_motor_stops(self, timeout: float | None = None) -> bool:
        """Block until the motor stops. Returns False if the timeout passed first."""
        return self._motor_stopped.wait(timeout)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _set_motor_running(self, running: bool) -> None:
        self._motor_running = running
        if running:
            self._motor_stopped.clear()
        else:
            self._motor_stopped.set()

    def _motor_reply(self, data: dict) -> MotorStatus:
        status = MotorStatus(
            running=data["running"], remaining_s=data["remaining_s"], angle=data.get("angle")
        )
        self._set_motor_running(status.running)
        if status.angle is not None:
            self._angle = status.angle
        return status

    def _request(self, cmd: str, timeout: float | None = None, **fields: Any) -> dict:
        if not self._ser:
            raise RuntimeError("not connected; call open() first")

        req_id = next(self._ids)
        slot = {"done": threading.Event(), "reply": None}
        with self._pending_lock:
            self._pending[req_id] = slot

        line = json.dumps({"id": req_id, "cmd": cmd, **fields}, separators=(",", ":")) + "\n"
        with self._write_lock:
            self._ser.write(line.encode())

        if not slot["done"].wait(timeout or self.reply_timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"no reply from ESP32 to '{cmd}'")

        reply = slot["reply"]
        if not reply.get("ok"):
            raise Esp32Error(reply.get("error") or f"'{cmd}' failed")
        return reply.get("data") or {}

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._ser.readline()
            except (serial.SerialException, OSError, TypeError) as e:
                if not self._stop.is_set():
                    log.error("serial connection lost: %s", e)
                return
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace").strip()
            if not text.startswith("{"):
                continue  # boot messages from the ESP32 chip etc.
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            self._handle(msg)

    def _handle(self, msg: dict) -> None:
        if msg.get("type") == "reply":
            with self._pending_lock:
                slot = self._pending.pop(msg.get("id"), None)
            if slot:
                slot["reply"] = msg
                slot["done"].set()
            return

        if msg.get("type") == "event":
            if msg.get("angle") is not None:      # "servo" and "motor" events carry the angle
                self._angle = msg["angle"]
            running = None
            if msg.get("event") == "motor":
                running = bool(msg.get("running"))
            elif msg.get("event") == "ready":
                running = False  # the ESP32 just booted, so the motor is off
            if running is not None:
                self._motor_running = running
            # Run the callback before waking wait_until_motor_stops(), so the
            # event is reported before the waiting code carries on.
            if self.on_event:
                try:
                    self.on_event(msg)
                except Exception:
                    log.exception("on_event callback failed")
            if running is not None:
                self._set_motor_running(running)
