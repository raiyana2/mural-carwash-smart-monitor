# Mural Car Wash Smart Monitor

Offline prototype that centralizes the controls of an 11-bay self-serve car wash and collects its data. A local computer (Windows, Mac, Linux or a Raspberry Pi) hosts the dashboard; an ESP32 plugged into it over USB runs Bay 1.

```
Browser dashboard ──HTTP──> local computer (pi/carwash) ──USB serial──> ESP32 (firmware/motor_rain_controller) ──> servo, sensors, button, LCD
```

Bay 1's parts stand in for real car wash equipment:

| Part on the ESP32 | Stands for |
|---|---|
| Servo | The bay's water valve. Open (180°) = the bay is **active**; closed (0°) = idle. |
| Rain sensor | A water flow meter: fully wet = 10 L/min. Low flow means the pump or nozzle may be failing; none means it isn't working. |
| Ultrasonic sensor | A car counter: each car that pulls into the bay is counted once. |
| Button | The coin slot: each press buys one minute. |
| LCD | The price ($1.00 per minute, up to 60 minutes), then during a wash the price paid and the time left. |

- Bays 2–11 are explicitly labelled local simulator bays, with 30 days of simulated history so the reports and repair predictions have data from the start.
- FastAPI serves the local dashboard (Operations, Bay Manager, Analytics, Maintenance); SQLite stores bay settings, washes, sensor readings, and fault history.
- Payments and multi-bay data are simulated. The application does not connect to a real payment system.

| Folder | What's in it |
|---|---|
| `firmware/motor_rain_controller/` | ESP32 firmware: the code in `src/main.cpp` and its pins/limits in `config.h`. PlatformIO builds `src/main.cpp` directly; the empty `motor_rain_controller.ino` lets the Arduino IDE open the same code. Documented below. |
| `platformio.ini` | PlatformIO project settings (board, libraries); points at the firmware folder |
| `pi/` | The dashboard server (the folder kept its name from when it ran on a Raspberry Pi): the `carwash` package (web interface, controllers, storage, ESP32 serial client), command-line tools, and tests. See [Setup, architecture, and launch](./pi/README.md). |

## Firmware

**Build and upload:** open the repo folder in VS Code with the **PlatformIO** extension, then click **Upload** (→), or run `pio run -t upload`. The board is the **Adafruit Feather ESP32 V2**. PlatformIO installs the libraries (ArduinoJson 7, ESP32Servo 3, LiquidCrystal_I2C) automatically. Only one program can use the port at a time: if the dashboard is running, press **Disconnect** on its Maintenance page before uploading, and close any serial monitor before connecting the dashboard.

**Arduino IDE instead:** open `firmware/motor_rain_controller/motor_rain_controller.ino`. That file is empty on purpose: the Arduino IDE also compiles the sketch's `src/` folder, so it builds the same `src/main.cpp` that PlatformIO does. Install ArduinoJson 7, ESP32Servo 3 and LiquidCrystal I2C (Marco Schwartz) from the Library Manager first.

### Wiring (pins in `firmware/motor_rain_controller/config.h`)

| ESP32 pin | Goes to | Notes |
|---|---|---|
| GPIO 25 | Servo signal (orange/yellow/white) | |
| GND | Servo GND (brown/black) **and** the servo supply's GND | All grounds must connect |
| — | Servo +5V (red) from a **separate 5 V supply** | A servo powered from the board's USB 5 V causes jitter or ESP32 restarts |
| 3V3 | Rain sensor VCC | **3.3 V, not 5 V** |
| GPIO 34 (A2) | Rain sensor AO | Analog wetness (0–4095, lower = wetter) |
| GPIO 39 (A3) | Rain sensor DO | LOW = wet. Set the threshold with the knob on the sensor board. |
| GPIO 15 | Ultrasonic (HC-SR04) TRIG | |
| GPIO 33 | Ultrasonic (HC-SR04) ECHO, **through a voltage divider** | The HC-SR04 runs on 5 V and its ECHO pin outputs 5 V. Use e.g. 1 kΩ from ECHO to the pin and 2 kΩ from the pin to GND, or a 3.3 V HC-SR04P. |
| GPIO 32 | Push button, other leg to GND | Internal pull-up; each press is sent to the dashboard |
| SDA (GPIO 22), SCL (GPIO 20) | I2C LCD 1602 SDA, SCL | Address `0x38` in `config.h` (other common ones: `0x27`, `0x3F`) |

`config.h` also sets `SERVO_REST_ANGLE` (0, where the servo goes at power-up), `SERVO_ACTIVE_ANGLE` (180, the default angle for `motor_on`), `SERVO_MIN_US`/`SERVO_MAX_US` (pulse widths), `MAX_RUN_SECONDS` (3600, one hour), `ULTRASONIC_TIMEOUT_US` and `BUTTON_DEBOUNCE_MS`.

### Protocol

Every message is one JSON object per line, at 115200 baud. Each request has an `id`, and the reply carries the same `id`:
`{"type":"reply","id":..,"ok":true/false,"error":null or "message","data":{..}}`

| Request | What it does | Reply `data` |
|---|---|---|
| `{"id":1,"cmd":"motor_on","seconds":5}` | **Timed run:** go to `SERVO_ACTIVE_ANGLE` (180°), hold 5 s, then back to the angle it was at before | `{"angle":180,"running":true,"remaining_s":5.0}` |
| `{"id":2,"cmd":"motor_on","seconds":5,"angle":90}` | Timed run at 90° | |
| `{"id":2,"cmd":"motor_on","seconds":300,"label":"Wash $5.00"}` | Timed run with LCD line 1 set to `label` (16 characters) for the run; the dashboard sends the price paid | |
| `{"id":3,"cmd":"motor_off"}` | End a timed run now, back to the angle it was at before | |
| `{"id":4,"cmd":"motor_status"}` | Is a run in progress, time left, angle | same as `motor_on` |
| `{"id":5,"cmd":"servo_set","angle":90}` | Move to 90° and **stay there** | `{"angle":90,"running":false,"remaining_s":0}` |
| `{"id":6,"cmd":"servo_status"}` | Same as `motor_status` | |
| `{"id":7,"cmd":"rain"}` | Read the rain sensor (average of 10 readings) | `{"raw":1830,"wet_percent":55.3,"raining":true}` |
| `{"id":8,"cmd":"distance"}` | Read the ultrasonic sensor | `{"distance_cm":42.7}` (`-1` = no echo, nothing in range) |
| `{"id":9,"cmd":"button"}` | Is the button held down right now? | `{"pressed":false}` |
| `{"id":10,"cmd":"lcd_text","row":0,"text":"Hello"}` | Set LCD row 0 or 1 (cut to 16 characters) | `{"row":0,"text":"Hello"}` |
| `{"id":11,"cmd":"lcd_clear"}` | Blank the LCD | |
| `{"id":12,"cmd":"lcd_backlight","on":false}` | LCD backlight on or off | `{"backlight":false}` |
| `{"id":13,"cmd":"ping"}` | Check the ESP32 is alive, and what this firmware has | `{"uptime_s":42.0,"firmware":"motor_rain_controller","version":2,"features":["motor","rain","distance","button","lcd"]}` |

Refusals (`ok:false`):
- angle not 0–180
- `seconds` not between 0 and 3600
- `motor_on` while a run is already in progress ("motor already running")
- `servo_set` during a timed run ("timed hold running; send motor_off first")
- `lcd_text` with a row other than 0 or 1

Events the ESP32 sends on its own:
- `{"type":"event","event":"ready"}` after boot
- `{"type":"event","event":"servo","angle":90,"running":false}` on every angle change
- `{"type":"event","event":"motor","running":true,"reason":"started","angle":180}`, and later `"running":false` with `"reason":"timer"` or `"stopped"`
- `{"type":"event","event":"button","pressed":true}` each time the button is pressed (debounced)

The timed run's timer runs on the ESP32, so the servo returns to its original angle on time even if the computer crashes or the USB cable is pulled. During a run the LCD shows the run's `label` (or "Motor running" without one) on line 1 and counts down the time left on line 2 by itself, e.g. "Wash $5.00 / 4:59 left"; when the run ends it goes back to the text last set with `lcd_text`.

The dashboard reads `features` from `ping` and only uses what the firmware has, so older firmware without the ultrasonic sensor, button or LCD still works (those parts just show as unavailable).
