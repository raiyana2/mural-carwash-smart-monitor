# ESP32 firmware: servo + rain sensor

`motor_rain_controller/` runs on the ESP32. The Raspberry Pi controls it over the USB cable, using the code in `../pi/`.

It does three things:
- **Moves a positional servo to an angle and holds it for a set number of seconds**, then returns it to its rest angle. The ESP32 does the timing itself, so the servo still returns on time if the Pi crashes or the cable is pulled.
- **Reads a rain sensor**, averaging 10 readings.
- **Reports whether the servo is holding (running)** and at what angle. It sends a message whenever it starts or stops, and answers status requests.

## Wiring

```
                      ┌──────────────────┐
 Raspberry Pi ──USB───┤ ESP32            │
                      │                  │
                      │ GPIO25 ──────────┼──────────────> servo SIGNAL (orange/yellow/white)
                      │ GND ─────────────┼──┐
                      │                  │  ├───────────> servo GND (brown/black)
                      │                  │  └── 5 V supply (−)
                      │                  │      5 V supply (+) ──> servo +5V (red)
                      │                  │
                      │ 3V3 ─────────────┼──> VCC  ┐
                      │ GND ─────────────┼──> GND  │ rain sensor
                      │ GPIO34 ──────────┼──> AO   │ comparator board (LM393)
                      │ GPIO35 ──────────┼──> DO   ┘   └── wires to the rain plate
                      └──────────────────┘
```

| Connection | Notes |
|---|---|
| ESP32 GPIO 25 → servo signal | The ESP32's 3.3 V signal works with most hobby servos |
| **Separate 5 V supply** → servo red / brown | Servos draw 0.5–2 A+ when moving. Powering one from the ESP32's 5V/VIN pin (from USB) causes jitter or ESP32 restarts. |
| Supply GND → **ESP32 GND** | Required. Without a shared ground the signal doesn't work. |
| ESP32 3V3 → rain sensor VCC | **Use 3.3 V, not 5 V**, so AO never goes above what the ESP32 pin can take |
| GPIO 34 → rain AO | Analog wetness value (ADC1 pins 32–39 only) |
| GPIO 35 → rain DO | LOW = wet. Adjust the threshold with the knob on the comparator board. |

Pins, angles and pulse widths are set in **`config.h`**:

| Setting | Default | Meaning |
|---|---|---|
| `SERVO_PIN` | 25 | Servo signal pin |
| `SERVO_REST_ANGLE` | 0 | Where the servo sits when off, and at power-up |
| `SERVO_ACTIVE_ANGLE` | 180 | Where `motor_on` moves it if no angle is sent |
| `SERVO_MIN_US` / `SERVO_MAX_US` | 500 / 2400 | Pulse widths for 0° and 180°. If the servo buzzes or strains at the ends, narrow this range (for example to 1000 / 2000). |
| `MAX_RUN_SECONDS` | 300 | Longest hold accepted |

Don't use GPIO 1 or 3 (the USB serial link), 6–11 (internal flash), or 0, 2, 12, 15 (they affect booting).

At power-up the servo moves to `SERVO_REST_ANGLE`, so keep that position clear.

## Flashing

1. Arduino IDE → **Boards Manager** → install **esp32 by Espressif Systems**.
2. **Library Manager** → install **ArduinoJson** (v7, by Benoit Blanchon) and **ESP32Servo** (by Kevin Harrington).
3. Open `motor_rain_controller/motor_rain_controller.ino` (the `config.h` file opens as a second tab).
4. Choose **Tools → Board → ESP32 Dev Module** and the right **Port**, then **Upload**. If it hangs on "Connecting…", hold the board's **BOOT** button.

To check it by hand, open the Serial Monitor (115200 baud, line ending "Newline") and type `{"id":1,"cmd":"motor_on","seconds":3,"angle":90}`. Close the Serial Monitor before running the Pi code, because only one program can use the port at a time.

## Protocol

Every message is one JSON object per line, at 115200 baud.

| Pi sends | ESP32 replies with `data` |
|---|---|
| `{"id":1,"cmd":"motor_on","seconds":5}` | `{"running":true,"remaining_s":5.0,"angle":180}` |
| `{"id":2,"cmd":"motor_on","seconds":5,"angle":90}` | `{"running":true,"remaining_s":5.0,"angle":90}` |
| `{"id":3,"cmd":"motor_off"}` | `{"running":false,"remaining_s":0,"angle":0}` |
| `{"id":4,"cmd":"motor_status"}` | `{"running":true,"remaining_s":3.2,"angle":90}` |
| `{"id":5,"cmd":"rain"}` | `{"raw":1830,"wet_percent":55.3,"raining":true}` |
| `{"id":6,"cmd":"ping"}` | `{"uptime_s":42.0}` |

Every reply has the form `{"type":"reply","id":<same id>,"ok":true/false,"error":null or "message","data":{...}}`.

The ESP32 also sends **events** without being asked:
- `{"type":"event","event":"ready"}` after it boots
- `{"type":"event","event":"motor","running":true,"reason":"started","angle":180}`
- `{"type":"event","event":"motor","running":false,"reason":"timer","angle":0}`, or `"stopped"` when stopped by command

It refuses `motor_on` if a hold is already running, if `seconds` isn't between 0 and `MAX_RUN_SECONDS`, or if `angle` isn't between 0 and 180.

"Running" means the firmware has sent the servo to its "on" angle and is holding it there. The servo has no feedback wire, so the ESP32 can't confirm that the servo physically got there.

## Rain sensor values

`raw` runs from 0 to 4095. On these modules, **lower means wetter**: about 4095 when dry, dropping as water bridges the plate. `wet_percent` is the same reading flipped onto a 0–100 scale. Exact values depend on your sensor, so check what it reads dry and soaked. `raining` comes from the DO pin and the threshold set by the board's knob.
