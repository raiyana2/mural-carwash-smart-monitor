# Raspberry Pi operations dashboard

The Raspberry Pi hosts a local FastAPI application and SQLite database. Its browser dashboard manages 11 bays:

| Bay | Source | Data and control |
|---|---|---|
| 1 | Hardware | One `Esp32Link` instance communicates with the real ESP32 over USB serial. Rain, motor state and angle, ping, and the timed 180° run come from the firmware protocol (see the [main README](../README.md)). The documented physical bay has no vehicle, wash-water flow, or independent motor-feedback sensor. |
| 2–11 | Simulator | Local Python simulator mirrors the same 180° timed run, generates labelled demo activity and occasional faults. No serial hardware is used for these bays. |

`esp32_link.py` remains the only Python serial client. The web service uses its `open()`, `ping()`, `read_rain()`, `motor_status()`, `motor_on()` and `motor_off()` methods (and `servo_set(0)` only to put the servo back at rest before a run). A background worker owns that one Bay 1 connection, polls it, and retries after a disconnect; **Disconnect** pauses those retries until **Connect** is pressed. Serial calls run outside the browser request loop. The interactive CLI and command-line examples remain available.

The application safely initializes its SQLite schema (`bays`, `pricing_settings`, `sessions`, `faults`) at startup. Bay configuration, integer-cent prices, session records, and fault history persist across restarts. The default database is `pi/data/monitor.sqlite3`; set `MURAL_MONITOR_DB` to use another local path. All payments and revenue in this milestone are simulated. No external API, payment processor, CDN, or internet connection is used at runtime.

## Raspberry Pi OS installation and launch

From Raspberry Pi OS with the repository checked out at `~/mural-carwash-smart-monitor`:

```bash
sudo apt update
sudo apt install -y python3-venv
cd ~/mural-carwash-smart-monitor/pi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
sudo usermod -aG dialout "$USER"
```

Use Python 3.9 or newer (Raspberry Pi OS Bookworm's Python 3.11 is suitable). If you added your account to `dialout`, log out and back in before connecting the ESP32.

Run the local server:

```bash
cd ~/mural-carwash-smart-monitor/pi
source .venv/bin/activate
python -m uvicorn web_app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000> on the Raspberry Pi. This loopback-only default keeps control local to the Pi. Bay 1 attempts the `ESP32_PORT` environment variable when set, then uses USB auto-detection. To pick a port in the UI, open **Maintenance**, press **Scan ports**, press **Use** next to the board (`/dev/ttyUSB0` or `/dev/ttyACM0` on the Pi, `COM5`-style on Windows), then **Connect**. Only this web process should own that serial port; close the PlatformIO/Arduino serial monitor and any other serial client first.

On Windows (for development), the same steps are:

```powershell
cd C:\Repos\mural-carwash-smart-monitor\pi
py -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m uvicorn web_app:app --host 127.0.0.1 --port 8000
```

Each bay's controls in **Bay Manager** are a run time and two buttons. **Run 180°** turns the motor from 0° to 180°, holds it for the run time, then turns it back to 0°; **Return now** turns it back early. On Bay 1 this is the firmware's timed hold (`motor_on` with angle 180, `motor_off`), so the ESP32 enforces the timer and its 300-second maximum by itself. If the servo isn't at 0° when a run starts (for example after the command-line `angle` tool), the Pi sends it to 0° first so every run is the full 180° turn. **Return now** works whenever the motor is running, including a run started before the dashboard restarted. Bay 1 rain sensor values become **Unknown** when missing or stale. ESP32-reported motor status is command/controller state, not independent relay feedback.

## Demo simulator

The dashboard simulates Bays 2–11 locally as soon as the server starts. It seeds clearly labelled sample activity on a new database, then starts occasional simulated runs and equipment faults. Use **Test faults** on the Maintenance page to inject missing motor feedback, missing water detection, a run that overran its limit, or an operator test fault. These scenarios only affect simulator bays.

To test the original USB serial protocol and Bay 1 screen without physical hardware, run the existing fake ESP32 in another terminal:

```bash
cd ~/mural-carwash-smart-monitor/pi
source .venv/bin/activate
python fake_esp32.py
```

Copy the printed virtual serial path (for example `/dev/pts/3`) into the **Serial port** field on the Maintenance page and press **Connect**. This test connects the ordinary `Esp32Link` client to the simulator; it does not change Bay 1's configured hardware data source or provide real sensor evidence. Stop the fake device with Ctrl+C; the dashboard should mark Bay 1 offline and record a serial-loss incident after a successful connection has been lost.

## Development and tests

```bash
cd ~/mural-carwash-smart-monitor/pi
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

The test client dependency from `requirements-dev.txt` is only needed for API tests, not to run the dashboard.

## Existing command-line controls

```bash
python main.py                         # finds an ESP32 USB port
python main.py --port /dev/ttyUSB0
python example.py /dev/ttyUSB0
```

Interactive commands are `angle DEGREES`, `on SECONDS [ANGLE]`, `off`, `status`, `rain`, `watch [SECONDS]`, `ping`, `help`, and `quit`. See [Command-line tools and the serial client](#command-line-tools-and-the-serial-client) below.

## The three pages

| Page | What it's for |
|---|---|
| **Operations** | Live overview: how many bays are ready or running, the wash floor (each bay's motor position and countdown), what needs attention, and run counts. Select a bay to open it in Bay Manager. |
| **Bay Manager** | One card per bay with a run time, **Run 180°** and **Return now**. Bay 1 also shows the rain plate. A dial shows where the motor is (0° resting, 180° running). |
| **Maintenance** | The Bay 1 ESP32 connection (port picker, Connect / Disconnect / Ping, and the last connection error), open faults with **Resolve**, putting bays in or out of service, simulator test faults, and the fault log. |

Pricing, wash modes and analytics are no longer in the interface. Their API routes (`/api/pricing/preview`, `/api/bays/{n}/settings`, `/api/analytics`) still exist, and runs are still saved in SQLite.

The interface bundles IBM Plex Sans and Mono in `static/fonts` with their license and requires no font CDN. Pages are drawn once and then patched every second, so countdowns stay live without clearing what you type.

## Troubleshooting the ESP32 connection

When **Connect** fails, the Maintenance page shows the reason in red. What each one means:

| Message starts with | What to do |
|---|---|
| "No ESP32 found on USB" | The list shows every port the computer can see. If none looks like the board, try another USB cable (many are charge-only) and check the board's USB driver (CP210x or CH340/CH9102). Otherwise pick the port yourself. |
| "There is no port called …" | The port name is wrong for this computer: `COM5`-style on Windows, `/dev/ttyUSB0` or `/dev/ttyACM0` on the Pi. Press **Scan ports** and **Use** the right one. |
| "… is already in use by another program" | Close the PlatformIO/Arduino serial monitor, wait for any upload to finish, and make sure only one dashboard (or `main.py`) is running. |
| "No permission to open …" (Pi) | `sudo usermod -aG dialout $USER`, then log out and back in. |
| "… opened, but the ESP32 didn't answer" | The port works but the firmware isn't replying. Upload `src/main.cpp` (PlatformIO → Upload), press the board's RESET button, and connect again. The dashboard already pulsed the reset line once before giving up. |

To upload firmware while the dashboard is running, press **Disconnect** first. The dashboard then stays off the port until you press **Connect**, so the upload isn't interrupted.

If Bay 1 logs "The ESP32 restarted on its own", the board is browning out, usually because the servo is powered from the board's USB 5 V. Give the servo its own 5 V supply and connect its ground to the ESP32's GND.

## Command-line tools and the serial client

| File | What it is |
|---|---|
| `web_app.py`, `database.py`, `static/` | The web dashboard (FastAPI + SQLite + browser page) |
| `esp32_link.py` | The `Esp32Link` class: talks to the ESP32 over USB |
| `main.py` | Interactive control: type commands, see motor events live |
| `example.py` | A short script showing how to use `Esp32Link` |
| `controller.py`, `controller_demo.py` | A standalone controller for building other UIs |
| `fake_esp32.py` | A pretend ESP32 for testing without hardware (Linux/Pi only) |
| `tests/` | Dashboard tests |

### Interactive control

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

### In your own code

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

### Controller (for other UIs)

```
UI (buttons, labels)  ──actions──>  Controller  ──>  Esp32Link  ──USB──>  ESP32
        ▲                              │
        └──── on_change(state) ────────┘
```

The web dashboard has its own controller (`MonitorController` in `web_app.py`). `controller.py` is a lighter, standalone layer for building a *different* UI, such as a desktop window. The controller:
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

### Testing without an ESP32 (on the Pi or Linux)

```bash
python fake_esp32.py              # prints e.g. "listening on /dev/pts/3"
python main.py --port /dev/pts/3  # in a second terminal
```
