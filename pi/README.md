# Car wash dashboard

This folder is the local server: a FastAPI application and a SQLite database that run on the computer the ESP32 is plugged into. That can be a Windows, Mac or Linux PC, or a Raspberry Pi (the folder is still called `pi` from when it ran on one). Its browser dashboard manages 11 bays:

| Bay | Source | Data and control |
|---|---|---|
| 1 | Hardware | One `Esp32Link` instance talks to the real ESP32 over USB serial, using the firmware protocol in the [main README](../README.md). |
| 2–11 | Simulator | A local Python simulator runs the same timed washes, adds labelled demo activity, water flow readings and occasional faults, and starts each new database with 30 days of history. No serial hardware is used for these bays. |

## What the hardware stands for

| Part | In the car wash | What the dashboard does with it |
|---|---|---|
| Servo | The bay's water valve | **Start wash** opens it (0° → 180°) for the time paid for, then the ESP32 closes it. While it's open the bay is **active**. Every wash is one open/close cycle, used to predict valve service. |
| Rain sensor | Water flow meter | Read every 2 s and shown as litres per minute (fully wet = 10 L/min). Each wash records its average flow; low or no flow raises an alert and feeds the repair predictions. Water while the valve is closed raises a "water while idle" alert (below). |
| Ultrasonic sensor | Car counter | Anything within 30 cm is a car in the bay. A car is **counted only during a wash**: once per wash, when it's within 30 cm for 2 readings in a row. After that, when it's further than 30 cm (or out of range) for 2 readings in a row, the car has left: the wash ends early and the valve closes. A wash with no car within 30 cm runs its full time. Counts appear on Operations, Bay Manager and Analytics. Settings in `carwash/config.py`: `VEHICLE_PRESENT_CM` (30), `END_WASH_WHEN_CAR_LEAVES` (True). |
| Button | Coin slot | Each press buys one minute ($1.00): it starts a 1-minute wash, or adds a minute to the wash in progress (up to 60 minutes), and the LCD shows the new total price. Ignored when the bay is out of service or has an open fault. Adding time needs firmware version 3 (`motor_add`). |
| LCD | Price display | Between washes: "$1.00 per minute / Max 60 min" ("Out of service" or "See attendant" when the bay can't be used). During a wash: the price paid and the time left, e.g. "Wash $5.00 / 4:59 left"; the ESP32 counts down by itself. "Bay 1 offline" when the dashboard disconnects. |

The dashboard asks the firmware which parts it has (`features` in the `ping` reply) and only uses those, so older firmware still works.

## Pricing

By default every bay charges **$1.00 a minute**, for 1 to 60 minutes; a started minute counts as a full one. The price, the one-hour limit and the button's minute are in `carwash/config.py` (`PRICE_PER_MINUTE_CENTS`, `MAX_WASH_MINUTES`, `BUTTON_RUN_SECONDS`); the firmware enforces the same hour (`MAX_RUN_SECONDS` in `config.h`). Each bay's price is stored in the `pricing_settings` table, so a bay can be given its own price through `PUT /api/bays/{n}/settings`. A database made with the old default ($5.00 for 2 minutes, 5 minutes maximum) is moved to the new default when the server starts; bays with a price of their own are left alone.

## The four pages

| Page | What it's for |
|---|---|
| **Operations** | Live overview: how many bays are ready or active, the wash floor (each bay's valve, countdown and cars counted today), what needs attention, and today's washes, revenue and cars. Select a bay to open it in Bay Manager. |
| **Bay Manager** | One card per bay: a wash time in minutes with its price, **Start wash**, **+1 min** (adds a minute to the wash in progress) and **Stop wash**. Each card shows the water flow and the car count; Bay 1's also shows what the button buys and what the LCD is showing. A dial shows the valve (closed = idle, open = active). |
| **Analytics** | Financial reports from the database for the last 7, 30 or 90 days: revenue, washes, average sale, cars counted and the share of cars that bought a wash, each compared with the period before (when the database goes back that far). Charts of revenue by day, revenue by bay and washes by hour, a per-bay table with utilization, the daily figures, and **Download CSV**. Hover or tab to a bar for its numbers. |
| **Maintenance** | **Equipment health** (below), the Bay 1 ESP32 connection (port picker, Connect / Disconnect / Ping, and the last connection error), open faults with **Resolve**, putting bays in or out of service, simulator test faults, and the fault log. |

## Predicting repairs

The **Equipment health** table on the Maintenance page rates three pieces of equipment per bay, from its sensor history (`carwash/controllers/maintenance.py`):

| Equipment | Measured by | Status |
|---|---|---|
| Water flow (pump and nozzle) | Average flow of each wash. The bay's normal flow is the median of its first 20 washes after a service; its current flow is the average of the last 5. | **Not functioning**: no water in the last wash (under 0.5 L/min). **May be failing**: under 70% of normal. **Service soon**: a falling trend (least-squares over the last 14 days) reaches 70% within 7 days; further out it shows the predicted date. |
| Valve (servo) | Open/close cycles since its last service, and this week's washes per day; Bay 1's flow meter while the valve is closed | **Service soon** when the 1,000-cycle service is due within 7 days (the date is predicted from the current rate). **May be failing** while water flows with the valve closed (stuck open or leaking). **Not functioning** if it was told to open and didn't respond. |
| Car counter (ultrasonic) | Cars counted against washes in the last 24 hours | **Not functioning**: 3+ washes and no cars counted. **May be failing**: 5+ washes and fewer than half as many cars. |

Problems also raise warnings under **Open faults** (and on Operations): "No water flow", "Low water flow", "Valve service due" and "Water while idle". On Bay 1, a wash with no water at all raises the alert 10 seconds in, without waiting for the wash to end. **Water while idle:** when no wash is running and Bay 1's sensor reads at least 1 L/min (10% wet) for 10 seconds, the valve may be stuck open or leaking. It's ignored for 30 seconds after a wash, while the sensor plate drips and dries, and it clears by itself after 10 seconds dry. Settings: `IDLE_WATER_LPM`, `IDLE_WATER_SECONDS`, `IDLE_WATER_GRACE_SECONDS` in `carwash/config.py`. The alerts clear themselves when the readings recover. After a repair, press **Mark serviced** on that piece of equipment: its history starts afresh and its alerts are resolved.

The simulated history includes some problems to show this off: Bay 10's pump has failed (no water), Bay 5's nozzle is clogging (low flow), Bay 8's pump is wearing (a predicted repair date a week or two out), and the busy Bay 7's valve is due for service. Bay 1 has no history until you run washes on it. The limits are in `carwash/config.py` (`FLOW_*`, `VALVE_SERVICE_CYCLES`, `SIM_HISTORY_DAYS`).

## Architecture

```
pi/
├── web_app.py              launcher kept for `uvicorn web_app:app` (imports carwash.web.app)
├── carwash/                the application, in layers; each layer only uses the ones below it
│   ├── config.py           every shared setting: bay count, price, 1-hour limit, angles, flow limits, timings, paths
│   ├── web/                ── interface ── no logic of its own
│   │   ├── app.py          FastAPI routes; turns ControllerError into HTTP 404/409/422/503
│   │   ├── schemas.py      request bodies the API accepts
│   │   └── static/         the browser page (index.html, app.js, charts.js, style.css, fonts)
│   ├── controllers/        ── logic ──
│   │   ├── monitor.py      MonitorController: the one entry point the web layer calls
│   │   ├── hardware_bay.py HardwareBay: Bay 1's serial link (connect, poll, wash, car count, flow, LCD)
│   │   ├── simulator.py    Simulator: Bays 2-11, their 30-day history, and the test faults
│   │   ├── maintenance.py  MaintenanceAnalyzer: equipment health, repair predictions and alerts
│   │   ├── pricing.py      the price of a wash
│   │   ├── errors.py       ControllerError (kinds: not_found, invalid, conflict, unavailable)
│   │   └── esp32_controller.py  standalone Controller for other UIs (not used by the dashboard)
│   ├── storage/
│   │   └── database.py     SQLite: bays, prices, washes, sensor readings, faults, services, reports
│   └── hardware/
│       └── esp32_link.py   Esp32Link: the USB serial client for the firmware's JSON protocol
├── tools/                  command-line programs: cli.py, example.py, controller_demo.py, fake_esp32.py
├── tests/                  one test file per layer, plus an end-to-end run against fake_esp32.py
└── data/                   the SQLite database (not in git)
```

```
browser ──HTTP──> web/app.py ──> MonitorController ──┬──> HardwareBay ──> Esp32Link ──USB──> ESP32 (Bay 1)
                                                    ├──> Simulator                              (Bays 2-11)
                                                    ├──> MaintenanceAnalyzer
                                                    └──> MonitorDatabase ──> data/monitor.sqlite3
```

Where to make a change:

| To change… | Edit |
|---|---|
| The price, a limit, angle, timing, alert level or the number of bays | `carwash/config.py` |
| What a page shows or how it looks | `carwash/web/static/app.js`, `style.css` (charts: `charts.js`) |
| A new API route | `carwash/web/app.py` (and `schemas.py` for its request body), calling a `MonitorController` method |
| Rules about when a bay may run | `carwash/controllers/monitor.py` |
| How Bay 1 talks to the ESP32, counts cars, measures flow or fills the LCD | `carwash/controllers/hardware_bay.py`, and `carwash/hardware/esp32_link.py` for the wire protocol |
| How repairs are predicted | `carwash/controllers/maintenance.py` |
| Simulated bays, their history or test faults | `carwash/controllers/simulator.py` |
| Stored data or the financial report | `carwash/storage/database.py` |

`esp32_link.py` remains the only Python serial client. A background worker owns the one Bay 1 connection, polls it, and retries after a disconnect; **Disconnect** pauses those retries until **Connect** is pressed. Serial calls run outside the browser request loop. The interactive CLI and command-line examples remain available.

The application safely creates or upgrades its SQLite schema (`bays`, `pricing_settings`, `sessions`, `faults`, `sensor_readings`, `equipment_service`, `meta`) at startup. Bay settings, integer-cent prices, washes, sensor readings, and fault history persist across restarts. The default database is `pi/data/monitor.sqlite3`; set `MURAL_MONITOR_DB` to use another path. All payments and revenue are simulated. No external API, payment processor, CDN, or internet connection is used at runtime.

### API

| Route | What it returns or does |
|---|---|
| `GET /api/dashboard` | Every bay's live state, plus today's washes, revenue and cars |
| `POST /api/bays/{n}/start` `{"seconds": 300}` | Start a wash (1–3600 s) |
| `POST /api/bays/{n}/add` `{"seconds": 60}` | Add time to the wash in progress (and to its price), up to the bay's maximum |
| `POST /api/bays/{n}/stop` | End a wash now |
| `GET /api/reports?days=30` | Financial report for the last 1–365 days: totals, the period before, today, and figures by day, bay and hour |
| `GET /api/maintenance` | Equipment health and repair predictions for every bay |
| `POST /api/maintenance/{n}/serviced` `{"equipment": "water"}` | Mark `water`, `valve` or `counter` serviced |
| `GET /api/faults`, `POST /api/faults/{id}/resolve` | Fault log; resolve a fault |
| `PUT /api/bays/{n}/enabled` `{"enabled": false}` | Take a bay out of service or put it back |
| `GET /api/hardware/ports`, `POST /api/hardware/connect`, `/disconnect`, `/ping` | Bay 1's ESP32 connection |
| `POST /api/simulator/scenario` | Inject a test fault into a simulated bay |

Older routes (`/api/pricing/preview`, `/api/bays/{n}/settings`, `/api/analytics`) still exist.

## Installation and launch

On Windows, from the repository folder:

```powershell
cd C:\Repos\mural-carwash-smart-monitor\pi
py -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m uvicorn web_app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. To let other computers on your network open the dashboard, use `--host 0.0.0.0` and browse to `http://<this computer's IP>:8000` from them (Windows may ask to allow Python through the firewall). Anyone who can reach that address can start washes, so only do this on a network you trust.

On a Raspberry Pi or other Linux computer:

```bash
sudo apt update
sudo apt install -y python3-venv
cd ~/mural-carwash-smart-monitor/pi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
sudo usermod -aG dialout "$USER"      # then log out and back in, to be allowed to use the USB port
python -m uvicorn web_app:app --host 127.0.0.1 --port 8000
```

Use Python 3.9 or newer. (`web_app:app` and `carwash.web.app:app` are the same app; run either from the `pi` folder.)

Bay 1 tries the `ESP32_PORT` environment variable when set, then USB auto-detection. To pick a port in the UI, open **Maintenance**, press **Scan ports**, press **Use** next to the board (`COM5`-style on Windows, `/dev/ttyUSB0` or `/dev/ttyACM0` on Linux), then **Connect**. Only this web process should own that serial port; close the PlatformIO/Arduino serial monitor and any other serial client first.

On Bay 1, **Start wash** is the firmware's timed hold (`motor_on` with angle 180 and the price as its LCD label), so the ESP32 enforces the timer and its one-hour maximum by itself. If the servo isn't at 0° when a wash starts (for example after the command-line `angle` tool), the dashboard sends it to 0° first so the valve always opens fully. **Stop wash** works whenever the valve is open, including a wash started before the dashboard restarted. Readings that are missing or stale show as unknown. ESP32-reported motor status is command/controller state, not independent feedback from the servo.

## Demo simulator

The dashboard simulates Bays 2–11 as soon as the server starts. A new database gets 30 days of simulated washes, cars (including a few that drive through without buying) and water flow, so Analytics and Equipment health have something to show; this happens once per database. After that, simulated washes start now and then, record their flow and are checked like real ones. Use **Test faults** on the Maintenance page to inject missing motor feedback, missing water detection, a run that overran its limit, or an operator test fault. These only affect simulator bays. To start over, stop the server and delete `pi/data/monitor.sqlite3`.

To test Bay 1 without the hardware (Linux only), run the fake ESP32 in another terminal. It acts like the current firmware, with a flow meter that's wet while the valve is open and dries off after, a "car" that comes and goes on the ultrasonic sensor, and an LCD whose text it prints. Press Enter in its window to press the bay's button (or run `pkill -USR1 -f fake_esp32` from another terminal). Type `leak` and Enter (or `pkill -USR2 -f fake_esp32`) to make the sensor wet with the valve closed, to test the water-while-idle alert:

```bash
cd ~/mural-carwash-smart-monitor/pi
source .venv/bin/activate
python tools/fake_esp32.py
```

Copy the printed virtual serial path (for example `/dev/pts/3`) into the **Serial port** field on the Maintenance page and press **Connect**. Stop the fake device with Ctrl+C; the dashboard should mark Bay 1 offline.

## Development and tests

```bash
cd ~/mural-carwash-smart-monitor/pi
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest -v
```

Run it from the `pi` folder. The tests are split by layer: `test_database.py`, `test_esp32_link.py`, `test_controllers.py`, `test_api.py`, `test_reports_maintenance.py` (pricing, car counting, flow alerts, the simulated history, reports and predictions), and `test_end_to_end.py` (which runs the real serial client and dashboard against `tools/fake_esp32.py`; Linux only).

The test client dependency from `requirements-dev.txt` is only needed for API tests, not to run the dashboard.

The interface bundles IBM Plex Sans and Mono in `carwash/web/static/fonts` with their license, and draws its charts itself (`charts.js`), so it needs no CDN. Pages are drawn once and then patched every second, so countdowns stay live without clearing what you type.

## Existing command-line controls

```bash
python -m tools.cli                    # finds an ESP32 USB port
python -m tools.cli --port COM5        # or /dev/ttyUSB0
python -m tools.example COM5
```

Interactive commands are `angle DEGREES`, `on SECONDS [ANGLE]`, `off`, `status`, `rain`, `distance`, `button`, `lcd ROW TEXT`, `lcdclear`, `backlight on|off`, `watch [SECONDS]`, `ping`, `help`, and `quit`. See [Command-line tools and the serial client](#command-line-tools-and-the-serial-client) below.

## Troubleshooting the ESP32 connection

When **Connect** fails, the Maintenance page shows the reason in red. What each one means:

| Message starts with | What to do |
|---|---|
| "No ESP32 found on USB" | The list shows every port the computer can see. If none looks like the board, try another USB cable (many are charge-only) and check the board's USB driver (CP210x or CH340/CH9102). Otherwise pick the port yourself. |
| "There is no port called …" | The port name is wrong for this computer: `COM5`-style on Windows, `/dev/ttyUSB0` or `/dev/ttyACM0` on Linux. Press **Scan ports** and **Use** the right one. |
| "… is already in use by another program" | Close the PlatformIO/Arduino serial monitor, wait for any upload to finish, and make sure only one dashboard (or `tools/cli.py`) is running. |
| "No permission to open …" (Linux) | `sudo usermod -aG dialout $USER`, then log out and back in. |
| "… running older firmware that doesn't have the motor commands" | The board has an old firmware build. Press **Disconnect**, upload the current firmware with PlatformIO, then **Connect**. |
| "… opened, but the ESP32 didn't answer" | The port works but the firmware isn't replying. Upload the firmware (PlatformIO → Upload), press the board's RESET button, and connect again. The dashboard already pulsed the reset line once before giving up. |

To upload firmware while the dashboard is running, press **Disconnect** first. The dashboard then stays off the port until you press **Connect**, so the upload isn't interrupted.

If Bay 1 logs "The ESP32 restarted on its own", the board is browning out, usually because the servo is powered from the board's USB 5 V. Give the servo its own 5 V supply and connect its ground to the ESP32's GND.

## Command-line tools and the serial client

| File (in `tools/`) | What it is | Run it from `pi/` with |
|---|---|---|
| `cli.py` | Interactive control: type commands, see motor events live | `python -m tools.cli` |
| `example.py` | A short script showing how to use `Esp32Link` | `python -m tools.example` |
| `controller_demo.py` | Shows how another UI would use the standalone `Controller` | `python -m tools.controller_demo` |
| `fake_esp32.py` | A pretend ESP32 for testing without hardware (Linux only) | `python tools/fake_esp32.py` |

### Interactive control

```bash
python -m tools.cli                    # finds the ESP32 automatically
python -m tools.cli --port COM5        # Windows (check Device Manager for the COM number)
python -m tools.cli --port /dev/ttyUSB0
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

Commands: `angle DEGREES`, `on SECONDS [ANGLE]`, `off`, `status`, `rain`, `distance`, `button`, `lcd ROW TEXT`, `lcdclear`, `backlight on|off`, `watch [SECONDS]`, `ping`, `help`, `quit`. Button presses are printed as they happen.

`angle` moves the servo and leaves it there. `on` is a timed hold: it moves the servo to ANGLE (0–180; default set by `SERVO_ACTIVE_ANGLE` in the firmware), holds it for SECONDS, then the ESP32 returns it to the angle it was at before the hold started. `angle` is refused while a timed hold is running.

### In your own code

```python
from carwash.hardware.esp32_link import Esp32Link, Esp32Error

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
    esp.read_distance()                  # -> 42.7 (cm), or None when nothing is in range
    esp.read_button()                    # -> True while the button is held down
    esp.lcd_text(0, "Bay 1 ready")       # LCD row 0 or 1, 16 characters
    esp.features                         # -> {"motor", "rain", "distance", "button", "lcd"}
    esp.ping()                           # ESP32 uptime in seconds
```

To react the moment the motor starts or stops, or the button is pressed, pass `on_event=your_function`. It gets each event as a dict, like `{"event": "motor", "running": False, "reason": "timer"}` or `{"event": "button", "pressed": True}`. Don't send requests from inside `on_event`: it runs on the thread that delivers replies, so a request there would wait forever. Set a flag and handle it elsewhere.

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

`carwash/controllers/esp32_controller.py` has `Controller`, a light, standalone layer for one ESP32, for building a *different* UI such as a desktop window. (The web dashboard uses `MonitorController` instead; see [Architecture](#architecture).) `Controller`:
- keeps **one up-to-date snapshot** of everything (`ControllerState`)
- **calls your function whenever it changes**, including once a second while the servo is running, so a countdown can update
- has **button-style actions** that never crash. They return `ActionResult(ok, message)`, ready to show to the user.
- **reads the rain sensor** in the background, every 2 s by default
- **reconnects by itself** if the cable is pulled or the ESP32 restarts
- keeps a **log** of what happened (connected, started, returned, errors)

```python
from carwash.controllers.esp32_controller import Controller

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
| `distance_cm` / `has_distance` | `42.7` / `True` | Ultrasonic distance (None = nothing in range) |
| `last_button_at` | `datetime` | When the bay's button was last pressed |
| `rain_updated_at` | `datetime` | "last updated" text |
| `log` | tuple of `LogEntry(time, message, level)` | Event list; level is `info`, `warning` or `error` |

Actions return `ok=False` with a readable message when something's wrong: `"Not connected to the ESP32"`, `"motor already running"`, `"angle must be 0-180"`, and so on.

**Threads:** `on_change` runs on the controller's background threads. Most UI toolkits only allow widget updates from their own thread, so hand the update over. In Tkinter that's `root.after(0, lambda: redraw(state))`; in asyncio it's `loop.call_soon_threadsafe(...)`.

```bash
python -m tools.controller_demo         # see it in action (or: python -m tools.controller_demo COM5)
```

### Testing without an ESP32 (Linux)

```bash
python tools/fake_esp32.py              # prints e.g. "listening on /dev/pts/3"
python -m tools.cli --port /dev/pts/3   # in a second terminal
```
