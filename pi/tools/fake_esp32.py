"""fake_esp32.py: pretend ESP32 for testing the Pi code with no hardware (Linux / Raspberry Pi only).

    python tools/fake_esp32.py
    -> prints a port like /dev/pts/3
    python -m tools.cli --port /dev/pts/3      (in another terminal)

It speaks the same protocol as the firmware in firmware/motor_rain_controller: servo_set /
servo_status, timed runs (motor_on / motor_off / motor_status), rain, distance, button,
lcd_text / lcd_clear / lcd_backlight and ping, with the same events.

To make testing easy:
  * the rain value drifts slowly wet <-> dry
  * the distance swings between about 15 cm and 150 cm, so a "car" arrives and leaves
    every half minute or so
  * press Enter in this window to press the button (or, from another terminal:
    pkill -USR1 -f fake_esp32)
  * whatever the Pi puts on the LCD is printed here
"""
import json
import math
import os
import select
import signal
import sys
import time
import tty

MAX_RUN_SECONDS = 3600
REST_ANGLE, ACTIVE_ANGLE = 0, 180
LCD_COLS = 16
FEATURES = ["motor", "rain", "distance", "button", "lcd"]

master, slave = os.openpty()
tty.setraw(slave)
print(f"fake ESP32 listening on {os.ttyname(slave)}", flush=True)
print("press Enter here to press the bay's button", flush=True)

booted = time.monotonic()
servo = {"angle": REST_ANGLE, "running": False, "until": 0.0, "return_to": REST_ANGLE}
lcd = {"idle": ["ESP32 Ready", ""], "shown": None, "backlight": True, "run_label": "Motor running"}
stdin_open = sys.stdin is not None and sys.stdin.isatty()


def send(obj):
    os.write(master, (json.dumps(obj) + "\n").encode())


# ---------------- LCD ----------------
def lcd_screen():
    if servo["running"]:
        left = max(0, math.ceil(servo["until"] - time.monotonic()))
        return [lcd["run_label"], f"{left // 60}:{left % 60:02d} left"]
    return list(lcd["idle"])


def lcd_refresh():
    screen = lcd_screen()
    if screen != lcd["shown"]:
        lcd["shown"] = screen
        print(f"  LCD |{screen[0]:<{LCD_COLS}}|{screen[1]:<{LCD_COLS}}|", flush=True)


# ---------------- servo ----------------
def set_angle(angle):
    servo["angle"] = max(0, min(180, angle))
    send({"type": "event", "event": "servo", "angle": servo["angle"], "running": servo["running"]})


def motor_event(reason):
    send({"type": "event", "event": "motor", "running": servo["running"],
          "reason": reason, "angle": servo["angle"]})


def hold_stop(reason):
    if not servo["running"]:
        return
    servo["running"] = False
    set_angle(servo["return_to"])
    motor_event(reason)


def status():
    left = max(0.0, servo["until"] - time.monotonic()) if servo["running"] else 0.0
    return {"angle": servo["angle"], "running": servo["running"], "remaining_s": round(left, 3)}


# ---------------- sensors ----------------
def rain():
    t = time.monotonic() - booted
    wet = 50 + 45 * math.sin(t / 20)                     # slowly goes wet <-> dry
    raw = int(4095 - wet / 100 * 4095)
    return {"raw": raw, "wet_percent": round(wet, 1), "raining": wet > 60}


def distance():
    t = time.monotonic() - booted
    return {"distance_cm": round(82 + 68 * math.sin(t / 5), 1)}   # 14 cm .. 150 cm


def press_button():
    send({"type": "event", "event": "button", "pressed": True})


# ---------------- requests ----------------
def handle(req):
    cmd, ok, err, data = req.get("cmd"), True, None, {}
    if cmd == "servo_set":
        angle = req.get("angle", -1)
        if not isinstance(angle, int) or not 0 <= angle <= 180:
            ok, err = False, "angle must be between 0 and 180"
        elif servo["running"]:
            ok, err = False, "timed hold running; send motor_off first"
        else:
            set_angle(angle)
        data = status()
    elif cmd in ("servo_status", "motor_status"):
        data = status()
    elif cmd == "motor_on":
        s = float(req.get("seconds", 0))
        angle = int(req.get("angle", ACTIVE_ANGLE))
        if s <= 0 or s > MAX_RUN_SECONDS:
            ok, err = False, f"seconds must be > 0 and <= {MAX_RUN_SECONDS}"
        elif not 0 <= angle <= 180:
            ok, err = False, "angle must be between 0 and 180"
        elif servo["running"]:
            ok, err = False, "motor already running"
        else:
            servo.update(running=True, until=time.monotonic() + s, return_to=servo["angle"])
            lcd["run_label"] = str(req.get("label") or "Motor running")[:LCD_COLS]
            set_angle(angle)
            motor_event("started")
        data = status()
    elif cmd == "motor_off":
        hold_stop("stopped")
        data = status()
    elif cmd == "rain":
        data = rain()
    elif cmd == "distance":
        data = distance()
    elif cmd == "button":
        data = {"pressed": False}
    elif cmd == "lcd_text":
        row = req.get("row", 0)
        if row not in (0, 1):
            ok, err = False, "row must be 0 or 1"
        else:
            lcd["idle"][row] = str(req.get("text", ""))[:LCD_COLS]
            data = {"row": row, "text": lcd["idle"][row]}
    elif cmd == "lcd_clear":
        lcd["idle"] = ["", ""]
    elif cmd == "lcd_backlight":
        lcd["backlight"] = bool(req.get("on", True))
        data = {"backlight": lcd["backlight"]}
    elif cmd == "ping":
        data = {"uptime_s": round(time.monotonic() - booted, 1), "firmware": "motor_rain_controller",
                "version": 2, "features": FEATURES}
    elif cmd == "_press_button":                         # for tests: acts like a press of the real button
        press_button()
    else:
        ok, err = False, f"unknown cmd '{cmd}'"
    send({"type": "reply", "id": req.get("id"), "ok": ok, "error": err, "data": data})


signal_presses = []
signal.signal(signal.SIGUSR1, lambda *_: signal_presses.append(True))

send({"type": "event", "event": "ready"})
set_angle(REST_ANGLE)
buf = b""
while True:
    if servo["running"] and time.monotonic() >= servo["until"]:
        hold_stop("timer")
    lcd_refresh()
    while signal_presses:
        signal_presses.pop()
        print("  button pressed", flush=True)
        press_button()
    watch = [master] + ([sys.stdin] if stdin_open else [])
    r, _, _ = select.select(watch, [], [], 0.05)
    if stdin_open and sys.stdin in r:
        if sys.stdin.readline() == "":
            stdin_open = False                           # stdin closed: stop watching it
        else:
            print("  button pressed", flush=True)
            press_button()
    if master not in r:
        continue
    buf += os.read(master, 4096)
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        try:
            handle(json.loads(line))
        except (json.JSONDecodeError, AttributeError):
            pass
