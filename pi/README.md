# Raspberry Pi side

This is Python code that controls the ESP32 (running `../esp32/motor_rain_controller`) over the USB cable.

| File | What it is |
|---|---|
| `esp32_link.py` | The `Esp32Link` class. Import it in your own code. |
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
  [motor RUNNING] started
  motor on for 5.0 s
esp32> status
  motor RUNNING, 3.1 s left
esp32> rain
  wet  55.3%  raw 1830  RAINING
esp32>
  [motor STOPPED] time is up
esp32> watch 2
  17:04:01  wet  55.1%  raw 1838  RAINING  motor off
  ...
```

Commands: `on SECONDS`, `off`, `status`, `rain`, `watch [SECONDS]`, `ping`, `help`, `quit`.

## In your own code

```python
from esp32_link import Esp32Link, Esp32Error

with Esp32Link() as esp:                 # or Esp32Link("COM5")
    esp.motor_on(5)                      # -> MotorStatus(running=True, remaining_s=5.0)
    esp.motor_status()                   # ask the ESP32
    esp.motor_running                    # last known state, no request sent
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

## Testing without an ESP32 (on the Pi or Linux)

```bash
python fake_esp32.py              # prints e.g. "listening on /dev/pts/3"
python main.py --port /dev/pts/3  # in a second terminal
```
