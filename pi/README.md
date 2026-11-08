# Raspberry Pi side

This is Python code that controls the ESP32 (running the firmware in `../src/`, see the [main README](../README.md)) over the USB cable.

| File | What it is |
|---|---|
| `esp32_link.py` | The `Esp32Link` class: talks to the ESP32 over USB |
| `controller.py` | The `Controller` class: the layer a **UI** should use (see below) |
| `controller_demo.py` | Shows how a UI would use the controller |
| `main.py` | Interactive control: type commands, see motor events live |
| `example.py` | A short script showing how to use `Esp32Link` |
| `fake_esp32.py` | A pretend ESP32 for testing without hardware (Linux/Pi only) |

## Setup

```bash
cd pi
pip install -r requirements.txt          # just pyserial
sudo usermod -aG dialout $USER           # Pi only: allow serial port access, then log out and back in
```

## Interactive control

```bash
python main.py                    # finds the ESP32 automatically
python main.py --port COM5        # Windows (check Device Manager for the COM number)
python main.py --port /dev/ttyUSB0
```

```
esp32> on 5
  [motor RUNNING] started (servo at 180°)
  servo at 180° for 5.0 s
esp32> status
  motor RUNNING at 180°, 3.1 s left
esp32>
  [motor STOPPED] time is up (servo at 0°)
esp32> on 3 90
  [motor RUNNING] started (servo at 90°)
  servo at 90° for 3.0 s
esp32> rain
  wet  55.3%  raw 1830  RAINING
esp32> watch 2
  17:04:01  wet  55.1%  raw 1838  RAINING  motor off
  ...
```

Commands: `angle DEGREES`, `on SECONDS [ANGLE]`, `off`, `status`, `rain`, `watch [SECONDS]`, `ping`, `help`, `quit`.

`angle` moves the servo and leaves it there. `on` is a timed hold: it moves the servo to ANGLE (0–180; default set by `SERVO_ACTIVE_ANGLE` in the firmware), holds it for SECONDS, then the ESP32 returns it to the angle it was at before the hold started. `angle` is refused while a timed hold is running.

## In your own code

```python
from esp32_link import Esp32Link, Esp32Error

with Esp32Link() as esp:                 # or Esp32Link("COM5")
    esp.servo_set(90)                    # move and stay -> MotorStatus(running=False, remaining_s=0, angle=90)
    esp.motor_on(5)                      # timed hold -> MotorStatus(running=True, remaining_s=5.0, angle=180)
    # esp.motor_on(5, angle=90)          # specific angle
    esp.motor_status()                   # ask the ESP32
    esp.motor_running                    # last known state, no request sent
    esp.angle                            # last known angle, no request sent
    esp.wait_until_motor_stops(timeout=10)
    esp.motor_off()
    esp.read_rain()                      # -> RainReading(raw=1830, wet_percent=55.3, raining=True)
    esp.ping()                           # ESP32 uptime in seconds
```

To react the moment the motor starts or stops, pass `on_event=your_function`. It gets each event as a dict, like `{"event": "motor", "running": False, "reason": "timer"}`.

| Error | Meaning |
|---|---|
| `Esp32Error` | The ESP32 refused, for example "motor already running" or seconds out of range |
| `TimeoutError` | No reply within 2 s (the cable came out, or the board is stuck) |

## Controller (for a UI)

```
UI (buttons, labels)  ──actions──>  Controller  ──>  Esp32Link  ──USB──>  ESP32
        ▲                              │
        └──── on_change(state) ────────┘
```

A UI should talk only to `Controller`, never to `Esp32Link` directly. The controller:
- keeps **one up-to-date snapshot** of everything (`ControllerState`)
- **calls your function whenever it changes**, including once a second while the servo is running, so a countdown can update
- has **button-style actions** that never crash. They return `ActionResult(ok, message)`, ready to show to the user.
- **reads the rain sensor** in the background, every 2 s by default
- **reconnects by itself** if the cable is pulled or the ESP32 restarts
- keeps a **log** of what happened (connected, started, returned, errors)

```python
from controller import Controller

ctl = Controller()                      # or Controller(port="COM5", rain_interval_s=2)
ctl.subscribe(on_change)                # on_change(state) is called on every change
ctl.start()                             # connects in the background, returns immediately

ctl.set_angle(45)                       # Angle slider  -> ActionResult(ok=True, message="Servo to 45°")
ctl.start_motor(5, angle=90)            # Start button  -> ActionResult(ok=True, message="Servo to 90° for 5 s")
ctl.stop_motor()                        # Stop button
ctl.refresh()                           # Refresh button (reads rain + status now)
ctl.state                               # current snapshot, any time

ctl.stop()                              # when the app closes
```

`ControllerState` fields:

| Field | Example | Show it as |
|---|---|---|
| `connected` | `True` | Connection indicator |
| `status_text` | `"Connected on COM5"`, `"Waiting for ESP32 (retrying every 3 s)"` | Status bar |
| `motor_running` | `True` | Running/stopped indicator; enable or disable buttons |
| `motor_angle` | `90` | Current angle |
| `motor_remaining_s` | `3.2` | Countdown |
| `rain` | `RainReading(raw=1830, wet_percent=55.3, raining=True)` or `None` | Rain gauge |
| `rain_updated_at` | `datetime` | "last updated" text |
| `log` | tuple of `LogEntry(time, message, level)` | Event list; level is `info`, `warning` or `error` |

Actions return `ok=False` with a readable message when something's wrong: `"Not connected to the ESP32"`, `"motor already running"`, `"angle must be 0-180"`, and so on.

**Threads:** `on_change` runs on the controller's background threads. Most UI toolkits only allow widget updates from their own thread, so hand the update over. In Tkinter that's `root.after(0, lambda: redraw(state))`; in asyncio it's `loop.call_soon_threadsafe(...)`.

```bash
python controller_demo.py               # see it in action (or: python controller_demo.py COM5)
```

## Testing without an ESP32 (on the Pi or Linux)

```bash
python fake_esp32.py              # prints e.g. "listening on /dev/pts/3"
python main.py --port /dev/pts/3  # in a second terminal
```
