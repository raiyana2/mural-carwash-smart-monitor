"""controller_demo.py: shows how a UI would use the Controller.

    python controller_demo.py              # auto-detect the ESP32
    python controller_demo.py COM5

A real UI would redraw its widgets in on_change() and call the actions from
buttons. Here we just print a status line each time something changes, and run
a short scripted sequence of "button presses".
"""
import sys
import time

from controller import Controller, ControllerState


def on_change(state: ControllerState) -> None:
    if not state.connected:
        print(f"[{state.status_text}]")
        return
    if state.motor_running:
        motor = f"servo at {state.motor_angle}°, {state.motor_remaining_s:4.1f}s left"
    else:
        motor = f"servo at {state.motor_angle}° (idle)"
    rain = (f"{state.rain.wet_percent:5.1f}% wet {'RAINING' if state.rain.raining else 'dry'}"
            if state.rain else "rain: -")
    print(f"{motor:32} | {rain}")


port = sys.argv[1] if len(sys.argv) > 1 else None

with Controller(port=port) as ctl:
    ctl.subscribe(on_change)

    print("waiting for the ESP32...")
    while not ctl.state.connected:
        time.sleep(0.2)

    print("\n> pressing 'Start' (3 s at 90°)")
    print("  ->", ctl.start_motor(3, angle=90))
    time.sleep(1)

    print("\n> pressing 'Start' again while it's running")
    print("  ->", ctl.start_motor(3))
    time.sleep(3)

    print("\n> pressing 'Start' (10 s), then 'Stop' after 2 s")
    print("  ->", ctl.start_motor(10))
    time.sleep(2)
    print("  ->", ctl.stop_motor())
    time.sleep(1)

    print("\nevent log:")
    for entry in ctl.state.log:
        print(f"  {entry.time:%H:%M:%S}  {entry.level:7}  {entry.message}")
