# mural-carwash-smart-monitor

A Raspberry Pi controls an ESP32 over a USB cable. The ESP32 drives a positional servo and reads a rain sensor.

```
Raspberry Pi (pi/)  ──USB serial──>  ESP32 (src/, PlatformIO)  ──>  servo + rain sensor
```

| Folder | What's in it |
|---|---|
| `src/main.cpp`, `include/config.h`, `platformio.ini` | ESP32 firmware (PlatformIO project) |
| `pi/` | Raspberry Pi code: `esp32_link.py`, the UI `controller.py`, the interactive `main.py`. See [`pi/README.md`](pi/README.md). |

## Firmware

**Build and upload:** open the repo folder in VS Code with the **PlatformIO** extension, then click **Upload** (→), or run `pio run -t upload`. The board is the **Adafruit Feather ESP32 V2**. PlatformIO installs the libraries (ArduinoJson 7, ESP32Servo 3) automatically. Close any serial monitor before running the Pi code, because only one program can use the port at a time.

### Wiring (pins in `include/config.h`)

| ESP32 pin | Goes to | Notes |
|---|---|---|
| GPIO 27 | Servo signal (orange/yellow/white) | |
| GND | Servo GND (brown/black) **and** the servo supply's GND | All grounds must connect |
| — | Servo +5V (red) from a **separate 5 V supply** | A servo powered from the board's USB 5 V causes jitter or ESP32 restarts |
| 3V3 | Rain sensor VCC | **3.3 V, not 5 V** |
| GPIO 34 | Rain sensor AO | Analog wetness (0–4095, lower = wetter) |
| GPIO 39 | Rain sensor DO | LOW = wet. Set the threshold with the knob on the sensor board. |

`config.h` also sets `SERVO_REST_ANGLE` (0, where the servo goes at power-up), `SERVO_ACTIVE_ANGLE` (180, the default angle for `motor_on`), `SERVO_MIN_US`/`SERVO_MAX_US` (pulse widths) and `MAX_RUN_SECONDS` (300).

### Protocol

Every message is one JSON object per line, at 115200 baud. Each request has an `id`, and the reply carries the same `id`:
`{"type":"reply","id":..,"ok":true/false,"error":null or "message","data":{..}}`

| Request | What it does | Reply `data` |
|---|---|---|
| `{"id":1,"cmd":"servo_set","angle":90}` | Move to 90° and **stay there** | `{"angle":90,"running":false,"remaining_s":0}` |
| `{"id":2,"cmd":"servo_status"}` | Current angle | same as above |
| `{"id":3,"cmd":"motor_on","seconds":5}` | **Timed hold:** go to `SERVO_ACTIVE_ANGLE`, hold 5 s, then back to the angle it was at before | `{"angle":180,"running":true,"remaining_s":5.0}` |
| `{"id":4,"cmd":"motor_on","seconds":5,"angle":90}` | Timed hold at 90° | |
| `{"id":5,"cmd":"motor_off"}` | End a timed hold now, back to the angle it was at before | |
| `{"id":6,"cmd":"motor_status"}` | Same as `servo_status` | |
| `{"id":7,"cmd":"rain"}` | Read the rain sensor (average of 10 readings) | `{"raw":1830,"wet_percent":55.3,"raining":true}` |
| `{"id":8,"cmd":"ping"}` | Check the ESP32 is alive | `{"uptime_s":42.0}` |

Refusals (`ok:false`):
- angle not 0–180
- `seconds` not between 0 and 300
- `motor_on` while a hold is already running ("motor already running")
- `servo_set` during a timed hold ("timed hold running; send motor_off first")

Events the ESP32 sends on its own:
- `{"type":"event","event":"ready"}` after boot
- `{"type":"event","event":"servo","angle":90,"running":false}` on every angle change
- `{"type":"event","event":"motor","running":true,"reason":"started","angle":180}`, and later `"running":false` with `"reason":"timer"` or `"stopped"`

The timed hold's timer runs on the ESP32, so the servo returns to its original angle on time even if the Pi crashes or the USB cable is pulled.
