# Raspberry Pi operations dashboard

The Raspberry Pi hosts a local FastAPI application and SQLite database. Its browser dashboard manages 11 bays:

| Bay | Source | Data and control |
|---|---|---|
| 1 | Hardware | One `Esp32Link` instance communicates with the real ESP32 over USB serial. Rain, motor state, ping, and motor commands come from the existing firmware protocol. The documented physical bay has no vehicle, wash-water flow, or independent motor-feedback sensor. |
| 2–11 | Simulator | Local Python simulator generates labelled demo customer sessions, timer and mode changes, sensor state, payments, and occasional faults. No serial hardware is used for these bays. |

`esp32_link.py` remains the only Python serial client. The web service uses its `open()`, `ping()`, `read_rain()`, `motor_status()`, `motor_on()` and `motor_off()` methods. A background worker owns that one Bay 1 connection, polls it, and retries after a disconnect. Serial calls run outside the browser request loop. The interactive CLI and command-line examples remain available.

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

Open <http://127.0.0.1:8000> on the Raspberry Pi. This loopback-only default keeps control local to the Pi. Bay 1 attempts the `ESP32_PORT` environment variable when set, then uses the existing USB VID auto-detection. To select a port in the UI, open **Bay 1** and enter its serial path (for example `/dev/ttyUSB0`) before connecting. Only this web process should own that serial port; close the Arduino Serial Monitor and any other serial client first.

Bay 1 start/stop controls call the original firmware protocol. The firmware remains responsible for the independent motor timer and its 300-second maximum. Bay 1 rain sensor values become **Unknown** when missing or stale, and vehicle/wash-flow sensors are shown as **Unavailable** because they are not part of the documented wiring. ESP32-reported motor status is command/controller state, not independent relay feedback.

## Demo simulator

The dashboard simulates Bays 2–11 locally as soon as the server starts. It seeds clearly labelled sample activity on a new database, then generates session arrivals, departures, timer activity, wash mode changes, sensor state, and occasional equipment faults. Use **Diagnostics** to manually trigger a customer arrival/departure, wash-mode change, purchased/granted-time anomaly, missing simulated motor feedback, missing simulated water detection, overrun, or operator test fault. These scenarios only affect simulator bays.

To test the original USB serial protocol and Bay 1 screen without physical hardware, run the existing fake ESP32 in another terminal:

```bash
cd ~/mural-carwash-smart-monitor/pi
source .venv/bin/activate
python fake_esp32.py
```

Copy the printed virtual serial path (for example `/dev/pts/3`) into the **Bay 1** connection field and select **CONNECT / RETRY**. This test connects the ordinary `Esp32Link` client to the simulator; it does not change Bay 1's configured hardware data source or provide real sensor evidence. Stop the fake device with Ctrl+C; the dashboard should mark Bay 1 offline and record a serial-loss incident after a successful connection has been lost.

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

Interactive commands remain `on SECONDS`, `off`, `status`, `rain`, `watch [SECONDS]`, `ping`, `help`, and `quit`.

## MURAL/OS terminal

MURAL/OS uses an amber and black terminal workspace with cyan section headers, rectangular equipment modules and a persistent function-key strip. Click the navigation buttons or use F1–F8 outside form fields: Overview, Bays, Pricing, Analytics, Diagnostics, Hardware, Events, Settings. At Raspberry Pi dimensions, the operations panels switch through touch tabs and the bay STOP command stays above the function keys.

Bay/01 is the physical USB/ESP32 unit. Its detail terminal exposes connection, ping, rain and commanded motor state. Missing, disconnected or stale measurements display **UNKNOWN**; sensors outside the documented hardware display **NOT INSTALLED**. The ESP32 still enforces its independent 300-second limit. Bays/02–11 retain labelled **TEST** links into simulator-only diagnostics.

Pricing uses an eleven-bay directory and one editor for the selected bay. The live payment preview uses unsaved editor rates; **QUOTE TIME** uses the existing API and saved rates. **SAVE CONFIGURATION** persists settings in SQLite. The anomaly demonstration compares the recorded purchased and granted seconds, including the signed deviation; it does not access a proprietary controller or infer a root cause.

All fonts and assets are local. IBM Plex Mono is bundled under the license in `static/fonts/OFL.txt`. Optional CRT scanlines can be toggled in Settings or the status line. Reduced-motion preferences disable blinking.

Sensor history contains at most 120 fresh readings received while the current browser tab is open. It starts empty and is not persisted. Analytics totals come from the existing SQLite report. Revenue over time uses only the latest ten sessions returned per bay and labels that sampling limit; utilization retains the backend's recorded-duration / 24-hour estimate. All payments are simulated.

See [redesign verification](docs/terminal-verification.md) for screenshots and verification scope.
