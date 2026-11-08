"""fake_esp32.py: pretend ESP32 for testing the Pi code with no hardware (Linux / Raspberry Pi only).

    python fake_esp32.py
    -> prints a port like /dev/pts/3
    python main.py --port /dev/pts/3      (in another terminal)

It speaks the same protocol as esp32/motor_rain_controller, including the motor
timer. The rain value drifts slowly so 'watch' has something to show.
"""
import json
import math
import os
import select
import time
import tty

MAX_RUN_SECONDS = 300
REST_ANGLE, ACTIVE_ANGLE = 0, 180

master, slave = os.openpty()
tty.setraw(slave)
print(f"fake ESP32 listening on {os.ttyname(slave)}", flush=True)

booted = time.monotonic()
motor = {"running": False, "until": 0.0, "angle": REST_ANGLE}


def send(obj):
    os.write(master, (json.dumps(obj) + "\n").encode())


def motor_event(reason):
    send({"type": "event", "event": "motor", "running": motor["running"],
          "reason": reason, "angle": motor["angle"]})


def status():
    left = max(0.0, motor["until"] - time.monotonic()) if motor["running"] else 0.0
    return {"running": motor["running"], "remaining_s": round(left, 3), "angle": motor["angle"]}


def rain():
    t = time.monotonic() - booted
    wet = 50 + 45 * math.sin(t / 20)                     # slowly goes wet <-> dry
    raw = int(4095 - wet / 100 * 4095)
    return {"raw": raw, "wet_percent": round(wet, 1), "raining": wet > 60}


def handle(req):
    cmd, ok, err, data = req.get("cmd"), True, None, {}
    if cmd == "motor_on":
        s = float(req.get("seconds", 0))
        angle = int(req.get("angle", ACTIVE_ANGLE))
        if s <= 0 or s > MAX_RUN_SECONDS:
            ok, err = False, f"seconds must be > 0 and <= {MAX_RUN_SECONDS}"
        elif not 0 <= angle <= 180:
            ok, err = False, "angle must be 0-180"
        elif motor["running"]:
            ok, err = False, "motor already running"
        else:
            motor.update(running=True, until=time.monotonic() + s, angle=angle)
            motor_event("started")
        data = status()
    elif cmd == "motor_off":
        was = motor["running"]
        motor.update(running=False, angle=REST_ANGLE)
        if was:
            motor_event("stopped")
        data = status()
    elif cmd == "motor_status":
        data = status()
    elif cmd == "rain":
        data = rain()
    elif cmd == "ping":
        data = {"uptime_s": round(time.monotonic() - booted, 1)}
    else:
        ok, err = False, f"unknown cmd '{cmd}'"
    send({"type": "reply", "id": req.get("id"), "ok": ok, "error": err, "data": data})


send({"type": "event", "event": "ready"})
buf = b""
while True:
    if motor["running"] and time.monotonic() >= motor["until"]:
        motor.update(running=False, angle=REST_ANGLE)
        motor_event("timer")
    r, _, _ = select.select([master], [], [], 0.05)
    if not r:
        continue
    buf += os.read(master, 4096)
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        try:
            handle(json.loads(line))
        except (json.JSONDecodeError, AttributeError):
            pass
