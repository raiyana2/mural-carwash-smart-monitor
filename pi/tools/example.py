"""example.py: using Esp32Link from your own script.

    python -m tools.example          # or: python -m tools.example COM5   (from the pi folder)
"""
import sys

from carwash.hardware.esp32_link import Esp32Link


def on_event(evt):
    if evt.get("event") == "motor":
        print(f"event: motor {'started' if evt['running'] else 'stopped'} ({evt['reason']})")


port = sys.argv[1] if len(sys.argv) > 1 else None

with Esp32Link(port, on_event=on_event) as esp:
    rain = esp.read_rain()
    print(f"rain sensor: {rain.wet_percent}% wet, raining={rain.raining}")

    print("running motor for 3 seconds...")
    esp.motor_on(3)
    print("status:", esp.motor_status())

    esp.wait_until_motor_stops(timeout=10)
    print("motor running?", esp.motor_running)
