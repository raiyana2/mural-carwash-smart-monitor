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

The dashboard simulates Bays 2–11 locally as soon as the server starts. It seeds clearly labelled sample activity on a new database, then generates session arrivals, departures, timer activity, wash mode changes, sensor state, and occasional equipment faults. Use **Fault simulation** to manually trigger a customer arrival/departure, wash-mode change, purchased/granted-time anomaly, missing simulated motor feedback, missing simulated water detection, overrun, or operator test fault. These scenarios only affect simulator bays.

To test the original USB serial protocol and Bay 1 screen without physical hardware, run the existing fake ESP32 in another terminal:

```bash
cd ~/mural-carwash-smart-monitor/pi
source .venv/bin/activate
python fake_esp32.py
```

Copy the printed virtual serial path (for example `/dev/pts/3`) into the **Bay 1** connection field and select **Connect ESP32**. This test connects the ordinary `Esp32Link` client to the simulator; it does not change Bay 1's configured hardware data source or provide real sensor evidence. Stop the fake device with Ctrl+C; the dashboard should mark Bay 1 offline and record a serial-loss incident after a successful connection has been lost.

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

## Industrial operations interface

The operations view uses a conceptual eleven-bay layout, pending confirmation of the actual floor plan. Its featured Bay 1 physical connection opens the live USB, ESP32 motor, and rain-sensor view. Bays 2–11 expose labelled **Test controls** links that preselect the bay in Fault Diagnostics. A pricing mismatch scenario displays purchased time, granted time, and their recorded difference. It does not infer a root cause.

The interface bundles IBM Plex Sans and Mono in `static/fonts` with their license and requires no font CDN. The compact Raspberry Pi layout keeps the map scrollable and provides touch-sized test controls. Operational counts come from the existing APIs; unavailable physical readings remain unknown. All payment figures remain simulated.
