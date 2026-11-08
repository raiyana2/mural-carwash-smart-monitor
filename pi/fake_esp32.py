"""fake_esp32.py: pretend ESP32 for testing the Pi code with no hardware (Linux / Raspberry Pi only).

    python fake_esp32.py
    -> prints a port like /dev/pts/3
    python main.py --port /dev/pts/3      (in another terminal)

It speaks the same protocol as the firmware in src/main.cpp: servo_set /
servo_status, timed holds (motor_on / motor_off / motor_status), rain and ping,
with the same events. The rain value drifts slowly so 'watch' has something to show.
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
servo = {"angle": REST_ANGLE, "running": False, "until": 0.0}   # running = timed hold active


def send(obj):
    os.write(master, (json.dumps(obj) + "\n").encode())


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
    set_angle(REST_ANGLE)
    motor_event(reason)


def status():
    left = max(0.0, servo["until"] - time.monotonic()) if servo["running"] else 0.0
    return {"angle": servo["angle"], "running": servo["running"], "remaining_s": round(left, 3)}


def rain():
    t = time.monotonic() - booted
    wet = 50 + 45 * math.sin(t / 20)                     # slowly goes wet <-> dry
    raw = int(4095 - wet / 100 * 4095)
    return {"raw": raw, "wet_percent": round(wet, 1), "raining": wet > 60}


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
            servo.update(running=True, until=time.monotonic() + s)
            set_angle(angle)
            motor_event("started")
        data = status()
    elif cmd == "motor_off":
        hold_stop("stopped")
        data = status()
    elif cmd == "rain":
        data = rain()
    elif cmd == "ping":
        data = {"uptime_s": round(time.monotonic() - booted, 1)}
    else:
        ok, err = False, f"unknown cmd '{cmd}'"
    send({"type": "reply", "id": req.get("id"), "ok": ok, "error": err, "data": data})


send({"type": "event", "event": "ready"})
set_angle(REST_ANGLE)
buf = b""
while True:
    if servo["running"] and time.monotonic() >= servo["until"]:
        hold_stop("timer")
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
