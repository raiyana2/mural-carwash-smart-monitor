"""Emulated wash bay that speaks the same MQTT contract as the real ESP32 firmware.

    python tools/fake_esp32.py --bay bay1 --host localhost
    python tools/fake_esp32.py --bay bay2 --fail-rate 0.2   # randomly reject some commands

Run several in separate terminals to emulate several bays.
"""
import argparse
import json
import random
import time

import paho.mqtt.client as mqtt

VALID = {"start", "stop", "pause", "resume", "emergency_stop", "reset", "set_output", "ping"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bay", default="bay1")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--prefix", default="carwash")
    ap.add_argument("--fail-rate", type=float, default=0.0)
    ap.add_argument("--delay", type=float, default=0.1, help="seconds before acking")
    a = ap.parse_args()

    base = f"{a.prefix}/bays/{a.bay}"
    state = {"mode": "idle", "program": None, "estop": False, "outputs": {}}

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"fake-{a.bay}")
    c.will_set(f"{base}/status", "offline", qos=1, retain=True)  # broker sends this if we die

    def on_connect(client, *_):
        client.subscribe([(f"{base}/cmd", 1), (f"{a.prefix}/bays/all/cmd", 1)])
        client.publish(f"{base}/status", "online", qos=1, retain=True)
        client.publish(f"{base}/state", json.dumps(state))
        print(f"[{a.bay}] online")

    def on_message(client, _, msg):
        cmd = json.loads(msg.payload)
        action, params = cmd.get("action"), cmd.get("params", {})
        time.sleep(a.delay)
        ok, err = True, None

        if action not in VALID:
            ok, err = False, f"unknown action {action}"
        elif random.random() < a.fail_rate:
            ok, err = False, "simulated fault"
        elif state["estop"] and action not in ("reset", "ping", "emergency_stop"):
            ok, err = False, "e-stop latched; send reset first"
        elif action == "start":
            if state["mode"] != "idle":
                ok, err = False, f"bay busy ({state['mode']})"
            else:
                state.update(mode="washing", program=params.get("program"))
        elif action == "stop":
            state.update(mode="idle", program=None)
        elif action == "pause" and state["mode"] == "washing":
            state["mode"] = "paused"
        elif action == "resume" and state["mode"] == "paused":
            state["mode"] = "washing"
        elif action == "emergency_stop":
            state.update(mode="idle", program=None, estop=True, outputs={})
        elif action == "reset":
            state["estop"] = False
        elif action == "set_output":
            state["outputs"][params["output"]] = bool(params["state"])

        print(f"[{a.bay}] {action} {params} -> {'ok' if ok else err}")
        client.publish(f"{base}/ack", json.dumps({"id": cmd["id"], "ok": ok, "error": err, "result": state}), qos=1)
        client.publish(f"{base}/state", json.dumps(state))

    c.on_connect, c.on_message = on_connect, on_message
    c.connect(a.host, a.port, 30)
    c.loop_forever()


if __name__ == "__main__":
    main()
