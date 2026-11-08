# ESP32 firmware: motor + rain sensor

`motor_rain_controller/` runs on the ESP32. The Raspberry Pi controls it over the USB cable, using the code in `../pi/`.

It does three things:
- **Runs a motor for a set number of seconds.** The ESP32 does the timing itself, so the motor stops on time even if the Pi crashes or the cable is pulled.
- **Reads a rain sensor**, averaging 10 readings.
- **Reports whether the motor is running.** It sends a message whenever the motor starts or stops, and answers status requests.

## Wiring

```
                      ┌──────────────────┐
 Raspberry Pi ──USB───┤ ESP32            │
                      │                  │
                      │ GPIO25 ──────────┼──> IN / SIG   ┐
                      │ GND ─────────────┼──> GND        │ motor driver / MOSFET /
                      │                  │               │ relay module ──> MOTOR
                      │                  │   motor PSU ──┘ (separate supply, e.g. 12 V)
                      │                  │
                      │ 3V3 ─────────────┼──> VCC  ┐
                      │ GND ─────────────┼──> GND  │ rain sensor
                      │ GPIO34 ──────────┼──> AO   │ comparator board (LM393)
                      │ GPIO35 ──────────┼──> DO   ┘   └── wires to the rain plate
                      └──────────────────┘
```

| ESP32 pin | Goes to | Notes |
|---|---|---|
| GPIO 25 | Motor driver IN/SIG | **Never a motor directly.** Set `MOTOR_ACTIVE_HIGH` to `false` for active-low relay boards. |
| GND | Driver GND **and** motor supply GND | All grounds must connect, or the signal won't work |
| 3V3 | Rain sensor VCC | **Use 3.3 V, not 5 V.** 5 V can push AO above what the ESP32 pin can take. |
| GPIO 34 | Rain sensor AO | Analog wetness value (ADC1 pins 32–39 only) |
| GPIO 35 | Rain sensor DO | LOW = wet. Adjust the threshold with the knob on the comparator board. |

To change pins, edit **`config.h`**. Don't use GPIO 1 or 3 (the USB serial link), 6–11 (internal flash), or 0, 2, 12, 15 (they affect booting). GPIO 34–39 are input-only, so they work for the sensor but not for the motor.

## Flashing

1. Arduino IDE → **Boards Manager** → install **esp32 by Espressif Systems**.
2. **Library Manager** → install **ArduinoJson** (v7, by Benoit Blanchon).
3. Open `motor_rain_controller/motor_rain_controller.ino` (the `config.h` file opens as a second tab).
4. Choose **Tools → Board → ESP32 Dev Module** and the right **Port**, then **Upload**. If it hangs on "Connecting…", hold the board's **BOOT** button.

To check it by hand, open the Serial Monitor (115200 baud, line ending "Newline") and type `{"id":1,"cmd":"rain"}`. Close the Serial Monitor before running the Pi code, because only one program can use the port at a time.

## Protocol

Every message is one JSON object per line, at 115200 baud.

| Pi sends | ESP32 replies with `data` |
|---|---|
| `{"id":1,"cmd":"motor_on","seconds":5}` | `{"running":true,"remaining_s":5.0}` |
| `{"id":2,"cmd":"motor_off"}` | `{"running":false,"remaining_s":0}` |
| `{"id":3,"cmd":"motor_status"}` | `{"running":true,"remaining_s":3.2}` |
| `{"id":4,"cmd":"rain"}` | `{"raw":1830,"wet_percent":55.3,"raining":true}` |
| `{"id":5,"cmd":"ping"}` | `{"uptime_s":42.0}` |

Every reply has the form `{"type":"reply","id":<same id>,"ok":true/false,"error":null or "message","data":{...}}`.

The ESP32 also sends **events** without being asked:
- `{"type":"event","event":"ready"}` after it boots
- `{"type":"event","event":"motor","running":true,"reason":"started"}`
- `{"type":"event","event":"motor","running":false,"reason":"timer"}`, or `"stopped"` when stopped by command

It refuses `motor_on` if the motor is already running, or if `seconds` isn't between 0 and `MAX_RUN_SECONDS` (default 300).

## Rain sensor values

`raw` runs from 0 to 4095. On these modules, **lower means wetter**: about 4095 when dry, dropping as water bridges the plate. `wet_percent` is the same reading flipped onto a 0–100 scale. Exact values depend on your sensor, so check what it reads dry and soaked. `raining` comes from the DO pin and the threshold set by the board's knob.

The rain plate corrodes faster when it's powered all the time. If that becomes a problem, power VCC from a spare GPIO pin and turn it on only while taking a reading.
