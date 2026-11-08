"""controller.py: the layer between a UI and the ESP32.

A UI (web page, desktop window, anything) should only talk to this class,
never to Esp32Link directly. The controller:

  * keeps ONE up-to-date picture of everything (ControllerState)
  * tells the UI whenever that picture changes (subscribe)
  * gives the UI simple actions that never raise (start_motor, stop_motor, ...)
  * reads the rain sensor in the background every few seconds
  * reconnects to the ESP32 by itself if the cable is pulled or it restarts

    from controller import Controller

    ctl = Controller()                       # or Controller(port="COM5")
    ctl.subscribe(lambda state: print(state))
    ctl.start()                              # connects in the background

    result = ctl.start_motor(5, angle=90)    # -> ActionResult(ok=True, message="...")
    ctl.set_angle(45)                        # move and stay (no timer)
    ctl.state                                 # -> ControllerState(...) snapshot
    ctl.stop()

Threads: callbacks run on the controller's background threads. Most UI
toolkits need you to hand the update over to their own thread (for example
Tkinter's root.after, or asyncio's loop.call_soon_threadsafe).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import serial

from esp32_link import Esp32Error, Esp32Link, RainReading

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# What the UI gets
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LogEntry:
    time: datetime
    message: str
    level: str = "info"          # "info" | "warning" | "error"


@dataclass(frozen=True)
class ControllerState:
    """A snapshot of everything the UI needs to draw. Never changes after it's made."""
    connected: bool = False
    port: str | None = None
    status_text: str = "Starting"          # short human-readable line for a status bar

    motor_running: bool = False
    motor_angle: int | None = None
    motor_remaining_s: float = 0.0         # counts down while running

    rain: RainReading | None = None
    rain_updated_at: datetime | None = None

    log: tuple[LogEntry, ...] = field(default_factory=tuple)   # newest last


@dataclass(frozen=True)
class ActionResult:
    """What a UI action returns: show `message` to the user, colour it by `ok`."""
    ok: bool
    message: str


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------
class Controller:
    def __init__(
        self,
        port: str | None = None,
        rain_interval_s: float = 2.0,
        reconnect_interval_s: float = 3.0,
        max_log: int = 50,
    ):
        self.port = port
        self.rain_interval_s = rain_interval_s
        self.reconnect_interval_s = reconnect_interval_s

        self._link: Esp32Link | None = None
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[ControllerState], None]] = []
        self._log: deque[LogEntry] = deque(maxlen=max_log)

        # current values (the snapshot is built from these)
        self._connected = False
        self._status_text = "Starting"
        self._motor_running = False
        self._motor_angle: int | None = None
        self._motor_ends_at = 0.0              # time.monotonic() when the hold ends
        self._rain: RainReading | None = None
        self._rain_at: datetime | None = None

        self._last_connect_error: str | None = None

        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._ticker: threading.Thread | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start connecting and polling in the background. Returns immediately."""
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="controller", daemon=True)
        self._worker.start()
        # Ticks once a second while the motor runs, so a countdown on screen updates.
        self._ticker = threading.Thread(target=self._tick, name="controller-tick", daemon=True)
        self._ticker.start()

    def stop(self) -> None:
        """Disconnect and stop the background threads. A running hold still finishes on the ESP32."""
        self._stop.set()
        for t in (self._worker, self._ticker):
            if t:
                t.join(timeout=3)
        self._disconnect("Stopped", level="info")

    def __enter__(self) -> "Controller":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # reading state
    # ------------------------------------------------------------------
    @property
    def state(self) -> ControllerState:
        with self._lock:
            remaining = max(0.0, self._motor_ends_at - time.monotonic()) if self._motor_running else 0.0
            return ControllerState(
                connected=self._connected,
                port=self._link.port if self._link else self.port,
                status_text=self._status_text,
                motor_running=self._motor_running,
                motor_angle=self._motor_angle,
                motor_remaining_s=round(remaining, 1),
                rain=self._rain,
                rain_updated_at=self._rain_at,
                log=tuple(self._log),
            )

    def subscribe(self, callback: Callable[[ControllerState], None]) -> Callable[[], None]:
        """Call `callback(state)` on every change. Returns a function that unsubscribes."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    # ------------------------------------------------------------------
    # actions (for buttons) — these never raise
    # ------------------------------------------------------------------
    def start_motor(self, seconds: float, angle: int | None = None) -> ActionResult:
        def do(link: Esp32Link) -> str:
            s = link.motor_on(seconds, angle)
            self._apply_motor(s.running, s.angle, s.remaining_s)
            return f"Servo to {s.angle}° for {seconds:g} s"
        return self._action(do)

    def set_angle(self, angle: int) -> ActionResult:
        """Move the servo to `angle` and leave it there (no timer)."""
        def do(link: Esp32Link) -> str:
            s = link.servo_set(angle)
            self._apply_motor(s.running, s.angle, s.remaining_s)
            return f"Servo to {s.angle}°"
        return self._action(do)

    def stop_motor(self) -> ActionResult:
        def do(link: Esp32Link) -> str:
            s = link.motor_off()
            self._apply_motor(s.running, s.angle, 0)
            return "Servo back to rest"
        # Not logged here: the ESP32's "stopped" event already adds a log line.
        return self._action(do, log_success=False)

    def refresh(self) -> ActionResult:
        """Read the rain sensor and motor status right now."""
        def do(link: Esp32Link) -> str:
            self._poll(link)
            return "Refreshed"
        return self._action(do, log_success=False)

    # ------------------------------------------------------------------
    # internals: actions
    # ------------------------------------------------------------------
    def _action(self, fn: Callable[[Esp32Link], str], log_success: bool = True) -> ActionResult:
        link = self._link
        if not (link and self._connected):
            return ActionResult(False, "Not connected to the ESP32")
        try:
            msg = fn(link)
        except (ValueError, Esp32Error) as e:          # bad input / ESP32 said no
            self._add_log(str(e), "warning")
            self._notify()
            return ActionResult(False, str(e))
        except (TimeoutError, serial.SerialException, OSError) as e:   # link trouble
            self._disconnect(f"Lost connection: {e}")
            return ActionResult(False, "Lost connection to the ESP32")
        if log_success:
            self._add_log(msg)
        self._notify()
        return ActionResult(True, msg)

    # ------------------------------------------------------------------
    # internals: background work
    # ------------------------------------------------------------------
    def _run(self) -> None:
        """Connect, then poll the rain sensor; reconnect whenever the link drops."""
        next_poll = 0.0
        while not self._stop.is_set():
            if not self._connected:
                if not self._connect():
                    self._stop.wait(self.reconnect_interval_s)
                    continue
                next_poll = 0.0

            link = self._link
            if link and not link.connected:              # reader thread died: cable pulled etc.
                self._disconnect("USB connection lost")
                continue

            if time.monotonic() >= next_poll:
                try:
                    self._poll(link)
                    self._notify()
                except (TimeoutError, serial.SerialException, OSError) as e:
                    self._disconnect(f"ESP32 stopped answering: {e}")
                    continue
                except Esp32Error as e:
                    self._add_log(f"Poll failed: {e}", "warning")
                next_poll = time.monotonic() + self.rain_interval_s

            self._stop.wait(0.2)

    def _tick(self) -> None:
        while not self._stop.wait(1.0):
            if self._motor_running:
                self._notify()                            # countdown changed

    def _connect(self) -> bool:
        link = Esp32Link(self.port, on_event=self._on_event)
        try:
            link.open()
        except (RuntimeError, TimeoutError, serial.SerialException, OSError) as e:
            # Short message for the status bar; the full error goes in the log once.
            detail = str(e)
            if detail != self._last_connect_error:
                self._last_connect_error = detail
                self._add_log(f"Can't connect: {detail}", "warning")
            self._set_status(f"Waiting for ESP32 (retrying every {self.reconnect_interval_s:g} s)")
            return False
        self._last_connect_error = None
        with self._lock:
            self._link = link
            self._connected = True
            self._status_text = f"Connected on {link.port}"
        self._add_log(f"Connected to ESP32 on {link.port}")
        try:
            s = link.motor_status()
            self._apply_motor(s.running, s.angle, s.remaining_s)
        except (TimeoutError, Esp32Error):
            pass
        self._notify()
        return True

    def _disconnect(self, reason: str, level: str = "error") -> None:
        with self._lock:
            link, self._link = self._link, None
            was_connected = self._connected
            self._connected = False
            self._status_text = reason
        if link:
            link.close()
        if was_connected:
            self._add_log(reason, level)
        self._notify()

    def _poll(self, link: Esp32Link) -> None:
        rain = link.read_rain()
        with self._lock:
            self._rain = rain
            self._rain_at = datetime.now()

    def _on_event(self, evt: dict) -> None:
        """Called by Esp32Link's reader thread for every event from the ESP32."""
        if evt.get("event") == "motor":
            running = bool(evt.get("running"))
            angle = evt.get("angle")
            with self._lock:
                self._motor_running = running
                self._motor_angle = angle
                if not running:
                    self._motor_ends_at = 0.0
            if running:
                # The reply to start_motor (with the time left) follows right after
                # this event and sends the update; notifying now would briefly show 0 s left.
                return
            why = {"timer": "time is up", "stopped": "stopped"}.get(evt.get("reason"), evt.get("reason"))
            self._add_log(f"Servo back to rest ({why})")
        elif evt.get("event") == "servo":
            # Sent on every angle change: manual servo_set moves and timed holds.
            running = bool(evt.get("running"))
            with self._lock:
                self._motor_angle = evt.get("angle")
                self._motor_running = running
                if not running:
                    self._motor_ends_at = 0.0
            if running:
                return  # start of a timed hold: the start_motor reply sends the update
        elif evt.get("event") == "ready":
            with self._lock:
                self._motor_running = False
            self._add_log("ESP32 restarted", "warning")
        self._notify()

    # ------------------------------------------------------------------
    # internals: state helpers
    # ------------------------------------------------------------------
    def _apply_motor(self, running: bool, angle: int | None, remaining_s: float) -> None:
        with self._lock:
            self._motor_running = running
            self._motor_angle = angle
            self._motor_ends_at = time.monotonic() + remaining_s if running else 0.0

    def _set_status(self, text: str) -> None:
        with self._lock:
            changed = text != self._status_text
            self._status_text = text
        if changed:
            self._notify()

    def _add_log(self, message: str, level: str = "info") -> None:
        log.log(logging.ERROR if level == "error" else logging.INFO, message)
        with self._lock:
            self._log.append(LogEntry(datetime.now(), message, level))

    def _notify(self) -> None:
        state = self.state
        with self._lock:
            subscribers = list(self._subscribers)
        for cb in subscribers:
            try:
                cb(state)
            except Exception:
                log.exception("subscriber callback failed")
