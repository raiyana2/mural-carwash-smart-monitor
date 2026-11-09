"""esp32_link.py: talk to the ESP32 motor/rain controller over USB serial.

    from esp32_link import Esp32Link

    with Esp32Link() as esp:            # finds the ESP32's USB port automatically
        esp.motor_on(5)                  # timed run: servo to 180° for 5 s, then back to where it was
        print(esp.motor_status())        # MotorStatus(running=True, remaining_s=4.9, angle=180)
        esp.wait_until_motor_stops()
        print(esp.read_rain())           # RainReading(raw=1830, wet_percent=55.3, raining=True)
        print(esp.read_distance())       # 42.7 (cm), or None when nothing is in range
        esp.lcd_text(0, "Bay 1 ready")   # first line of the LCD (16 characters)
        esp.servo_set(90)                # low-level: move to 90° and stay there
        esp.features                     # what this firmware has, e.g. {"motor", "rain", "distance", "button", "lcd"}

Button presses arrive as events: pass on_event and look for {"event": "button"}.

How it works: every request gets a number ("id"). A background thread reads
lines from the ESP32 and hands each reply to whoever is waiting for that id.
Lines that aren't replies are "events" (like the motor stopping on its own),
which update the motor state here and are passed to your on_event callback.

If the port can't be opened or the ESP32 doesn't answer, open() raises
Esp32ConnectError with a message that says what to check.
"""
from __future__ import annotations

import itertools
from collections import deque
import json
import logging
import os
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
# Boards that use the ESP32's own USB (S2/S3/C3/C6) instead of a USB-serial chip.
NATIVE_USB_VIDS = {0x303A}

FIRST_ANSWER_TIMEOUT_S = 2.5   # how long to wait for a reply before trying a reset
AFTER_RESET_TIMEOUT_S = 4.0    # how long to wait for the board to boot after that reset


class Esp32Error(Exception):
    """The ESP32 answered but refused the request (ok=false). The link itself is fine."""


class Esp32ConnectError(RuntimeError):
    """open() failed. str(error) explains what to check; `kind` is one of:
    not_found, missing_port, busy, permission, no_answer, open_failed."""

    def __init__(self, message: str, kind: str, port: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.port = port


@dataclass
class MotorStatus:
    running: bool        # True while a timed run (motor_on) is holding the servo
    remaining_s: float   # seconds left before it turns back
    angle: int | None = None  # current servo angle (0-180)


@dataclass
class RainReading:
    raw: int            # 0..4095 from the ADC; lower = wetter
    wet_percent: float  # 0 = dry, 100 = soaked
    raining: bool       # from the module's digital output (threshold set by its knob)


# ----------------------------------------------------------------------
# finding ports
# ----------------------------------------------------------------------
def list_serial_ports() -> list[dict]:
    """Every serial port on this computer, likely ESP32 boards first."""
    ports = []
    for p in list_ports.comports():
        ports.append({
            "device": p.device,
            "description": p.description if p.description and p.description != "n/a" else "",
            "vid": p.vid,
            "pid": p.pid,
            "likely_esp32": p.vid in ESP32_USB_VIDS,
        })
    ports.sort(key=lambda p: (not p["likely_esp32"], p["device"]))
    return ports


def describe_ports() -> str:
    ports = list_serial_ports()
    if not ports:
        return ("No serial ports were found at all. Check the USB cable (many cables are "
                "charge-only) and that the board's USB driver is installed.")
    names = ", ".join(
        p["device"] + (f" ({p['description']})" if p["description"] else "") + (" ← likely ESP32" if p["likely_esp32"] else "")
        for p in ports
    )
    return f"Serial ports found: {names}."


def find_esp32_port() -> str:
    ports = [p["device"] for p in list_serial_ports() if p["likely_esp32"]]
    if not ports:
        raise Esp32ConnectError(
            f"No ESP32 found on USB. {describe_ports()} Pick the board's port explicitly.",
            "not_found",
        )
    if len(ports) > 1:
        log.warning("several ESP32-like ports found %s; using %s", ports, ports[0])
    return ports[0]


def _is_native_usb(port: str) -> bool:
    return any(p["device"].lower() == port.lower() and p["vid"] in NATIVE_USB_VIDS for p in list_serial_ports())


def _explain_open_error(port: str, exc: Exception) -> Esp32ConnectError:
    text = str(exc)
    low = text.lower()
    if ("access is denied" in low or "busy" in low or "errno 16" in low
            or "exclusively lock" in low or "resource temporarily unavailable" in low):
        return Esp32ConnectError(
            f"{port} is already in use by another program. Close the PlatformIO/Arduino serial "
            "monitor (or any other app or terminal using the port), wait for any upload to finish, "
            "then connect again.",
            "busy", port,
        )
    if "permission denied" in low or "errno 13" in low:
        if os.name == "posix":
            return Esp32ConnectError(
                f"No permission to open {port}. Run: sudo usermod -aG dialout $USER, then log out and back in.",
                "permission", port,
            )
        return Esp32ConnectError(
            f"{port} is already in use by another program. Close the PlatformIO/Arduino serial monitor "
            "and try again.",
            "busy", port,
        )
    if ("no such file" in low or "cannot find the file" in low or "filenotfound" in low
            or "errno 2]" in low or "does not exist" in low):
        return Esp32ConnectError(f"There is no port called {port}. {describe_ports()}", "missing_port", port)
    return Esp32ConnectError(
        f"Could not open {port}: {text}. Try unplugging the USB cable and plugging it back in.",
        "open_failed", port,
    )


# ----------------------------------------------------------------------
# the link
# ----------------------------------------------------------------------
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
        # Older firmware doesn't say what it has; it only knew the motor and rain sensor.
        self.features: frozenset[str] = frozenset({"motor", "rain"})
        self.firmware_version: int | None = None
        self._bytes_received = 0                  # for diagnosing a silent board
        self._other_lines: deque[str] = deque(maxlen=6)   # recent non-JSON lines (boot / crash messages)

    # ------------------------------------------------------------------
    # connect / disconnect
    # ------------------------------------------------------------------
    def open(self) -> "Esp32Link":
        port = self.port or find_esp32_port()
        native = _is_native_usb(port)
        try:
            self._ser = self._open_port(port, native)
        except (serial.SerialException, OSError, ValueError) as exc:
            raise _explain_open_error(port, exc) from exc
        self.port = port

        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="esp32-reader", daemon=True)
        self._reader.start()

        if not self._wait_for_answer(FIRST_ANSWER_TIMEOUT_S):
            # Nothing answered. The board may be stuck (for example left in its bootloader
            # after an interrupted upload). Pulse its reset line once, like esptool does
            # after an upload, and give the firmware time to boot.
            if native or not self._pulse_reset() or not self._wait_for_answer(AFTER_RESET_TIMEOUT_S):
                self.close()
                raise Esp32ConnectError(self._no_answer_message(port), "no_answer", port)
        try:
            self.motor_status()  # learn the current motor state
        except Esp32Error as exc:
            self.close()
            if "unknown cmd" in str(exc):
                raise Esp32ConnectError(
                    f"The ESP32 on {port} answers, but it's running older firmware that doesn't "
                    "have the motor commands (motor_on / motor_off / motor_status). Upload the "
                    "current firmware/motor_rain_controller from this branch (press Disconnect here first, then "
                    "PlatformIO → Upload), and connect again.",
                    "old_firmware", port,
                ) from exc
            raise Esp32ConnectError(f"The ESP32 on {port} refused a status request: {exc}", "open_failed", port) from exc
        log.info("connected to ESP32 on %s", self.port)
        return self

    def _open_port(self, port: str, native: bool) -> serial.Serial:
        s = serial.Serial()
        s.port = port
        s.baudrate = self.baud
        s.timeout = 0.1
        # Boards with a USB-serial chip reset when RTS is high and DTR is low (the
        # auto-reset circuit esptool uses), which would also send the servo back to
        # its rest angle mid-run. Keep both low. Native-USB boards only send data
        # once the computer raises DTR, and raising it alone doesn't reset them.
        s.rts = False
        s.dtr = native
        lower_dtr_after_open = False
        if os.name == "posix":
            s.exclusive = True  # refuse to share the port with a second program
            if not native:
                # Linux raises DTR and RTS together when a port opens, and pyserial then
                # lowers DTR before RTS. For a moment that is "RTS high, DTR low", which
                # resets the board. Skipping pyserial's DTR step lowers RTS first instead,
                # then DTR is lowered below.
                s.dsrdtr = True
                lower_dtr_after_open = True
        s.open()
        if lower_dtr_after_open:
            s.dsrdtr = False
            try:
                s.dtr = False
            except OSError:
                pass  # virtual ports (fake_esp32.py) have no modem lines
        return s

    def _wait_for_answer(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.connected:
                return False
            try:
                self.ping(timeout=0.5)
                return True
            except TimeoutError:
                continue
        return False

    def _no_answer_message(self, port: str) -> str:
        """Explain a silent board using whatever it did (or didn't) print."""
        printed = " | ".join(self._other_lines)
        low = printed.lower()
        if self._bytes_received == 0:
            return (f"{port} opened, but nothing at all came back from it, not even after a reset. "
                    f"{port} may not be the ESP32 (Bluetooth serial ports look the same): unplug the "
                    f"board and see which port disappears. {describe_ports()}")
        if "brownout" in low:
            return (f"The ESP32 on {port} keeps restarting because its power dips (brownout). This is "
                    "almost always the servo drawing power from the board's USB 5 V: power the servo "
                    "from its own 5 V supply and connect that supply's GND to the ESP32's GND. "
                    f"The board printed: {printed}")
        if "guru meditation" in low or "backtrace" in low or "abort()" in low or "assert failed" in low:
            return (f"The firmware on {port} is crashing while it starts up, so it never gets as far "
                    f"as answering. The board printed: {printed}")
        if "waiting for download" in low or "download(" in low:
            return (f"The ESP32 on {port} is sitting in its bootloader (upload mode) instead of running "
                    f"the firmware. Press its RESET button and connect again. The board printed: {printed}")
        if "rst:" in low or "ets " in low or "boot:" in low:
            return (f"The ESP32 on {port} starts booting but the firmware never answers; it may be "
                    "restarting over and over, or the upload didn't finish. Upload again and watch for "
                    f"[SUCCESS]. The board printed: {printed}")
        return (f"{port} opened and the board sent data, but no reply to the dashboard's requests. Check "
                "that this project's firmware (firmware/motor_rain_controller) is uploaded and that no "
                f"serial monitor is open. The board printed: {printed or '(only unreadable bytes)'}")

    def _pulse_reset(self) -> bool:
        """Reset a board with the usual auto-reset circuit. Returns False if the port has no modem lines."""
        try:
            self._ser.dtr = False
            self._ser.rts = True     # EN low: the chip stops
            time.sleep(0.1)
            self._ser.rts = False    # EN high with IO0 high: normal boot into the firmware
        except (OSError, serial.SerialException):
            return False
        log.info("no answer on %s; pulsed the reset line", self.port)
        return True

    def close(self) -> None:
        self._stop.set()
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=1)
        if self._ser:
            try:
                self._ser.close()
            except (serial.SerialException, OSError):
                pass
        self._ser = None

    def __enter__(self) -> "Esp32Link":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def ping(self, timeout: float | None = None) -> float:
        """Returns the ESP32's uptime in seconds (and learns which features the firmware has)."""
        data = self._request("ping", timeout=timeout)
        if "features" in data:
            self.features = frozenset(data["features"])
            self.firmware_version = data.get("version")
        return data["uptime_s"]

    def has(self, feature: str) -> bool:
        """True if the firmware reported this feature ("motor", "rain", "distance", "button", "lcd")."""
        return feature in self.features

    def motor_on(self, seconds: float, angle: int | None = None, label: str | None = None) -> MotorStatus:
        """Move the servo to `angle` (default: the firmware's SERVO_ACTIVE_ANGLE, 180°),
        hold it for `seconds`, then the ESP32 turns it back to the angle it was at before, by itself.
        `label` (16 characters) is shown on the LCD's first line during the run, above the countdown."""
        if seconds <= 0:
            raise ValueError("seconds must be > 0")
        if angle is not None and not 0 <= angle <= 180:
            raise ValueError("angle must be 0-180")
        fields = {"seconds": seconds}
        if angle is not None:
            fields["angle"] = int(angle)
        if label:
            fields["label"] = str(label)[:16]
        return self._motor_reply(self._request("motor_on", **fields))

    def motor_off(self) -> MotorStatus:
        """End a timed run now (the servo turns back)."""
        return self._motor_reply(self._request("motor_off"))

    def motor_status(self) -> MotorStatus:
        return self._motor_reply(self._request("motor_status"))

    def servo_set(self, angle: int) -> MotorStatus:
        """Move the servo to `angle` (0-180) and leave it there (no timer).
        The ESP32 refuses this while a timed run (motor_on) is in progress."""
        if not 0 <= angle <= 180:
            raise ValueError("angle must be 0-180")
        return self._motor_reply(self._request("servo_set", angle=int(angle)))

    def servo_status(self) -> MotorStatus:
        """Same information as motor_status(): angle, and whether a timed run is in progress."""
        return self._motor_reply(self._request("servo_status"))

    @property
    def angle(self) -> int | None:
        """Last known servo angle (kept current by replies and events, no request sent)."""
        return self._angle

    def read_rain(self) -> RainReading:
        d = self._request("rain")
        return RainReading(raw=d["raw"], wet_percent=d["wet_percent"], raining=d["raining"])

    def read_distance(self) -> float | None:
        """Ultrasonic distance to the nearest object in cm, or None if nothing echoed back (out of range)."""
        cm = self._request("distance")["distance_cm"]
        return None if cm is None or cm < 0 else float(cm)

    def read_button(self) -> bool:
        """True while the button is held down. (Presses also arrive as "button" events.)"""
        return bool(self._request("button")["pressed"])

    def lcd_text(self, row: int, text: str) -> str:
        """Show `text` on LCD row 0 or 1 (cut to 16 characters). During a timed run the
        firmware shows its countdown instead and puts this text back when the run ends."""
        if row not in (0, 1):
            raise ValueError("row must be 0 or 1")
        return self._request("lcd_text", row=row, text=str(text))["text"]

    def lcd_clear(self) -> None:
        self._request("lcd_clear")

    def lcd_backlight(self, on: bool = True) -> None:
        self._request("lcd_backlight", on=bool(on))

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
        ser = self._ser
        if not ser:
            raise RuntimeError("not connected; call open() first")

        req_id = next(self._ids)
        slot = {"done": threading.Event(), "reply": None}
        with self._pending_lock:
            self._pending[req_id] = slot

        line = json.dumps({"id": req_id, "cmd": cmd, **fields}, separators=(",", ":")) + "\n"
        try:
            with self._write_lock:
                ser.write(line.encode())
        except BaseException:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise

        if not slot["done"].wait(timeout or self.reply_timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"no reply from ESP32 to '{cmd}'")

        reply = slot["reply"]
        if not reply.get("ok"):
            raise Esp32Error(reply.get("error") or f"'{cmd}' failed")
        return reply.get("data") or {}

    def _read_loop(self) -> None:
        # Bytes are collected until a newline arrives. (readline() would hand back half
        # a line whenever the 0.1 s read timeout fell in the middle of one, and that
        # reply would be lost.)
        buf = b""
        while not self._stop.is_set():
            ser = self._ser
            if ser is None:
                return
            try:
                chunk = ser.read(ser.in_waiting or 1)
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                if not self._stop.is_set():
                    log.error("serial connection lost: %s", e)
                return
            if not chunk:
                continue
            buf += chunk
            self._bytes_received += len(chunk)
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._handle_line(raw)
            if len(buf) > 4096:
                buf = b""  # a long run of bytes with no newline is noise, not JSON

    def _handle_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text.startswith("{"):
            if text:
                self._other_lines.append(text[:160])  # boot or crash messages: kept for diagnosis
            return
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return
        if isinstance(msg, dict):
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
