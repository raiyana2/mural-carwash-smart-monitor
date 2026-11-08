"""main.py: interactive control of the ESP32 from the Raspberry Pi.

    python main.py                  # auto-detect the ESP32's USB port
    python main.py --port COM5      # Windows
    python main.py --port /dev/ttyUSB0

Then type commands at the  esp32>  prompt:
    on 5        move the servo to its "on" angle, hold 5 seconds, then back to rest
    on 5 90     same, but to 90 degrees
    off         back to rest now
    status      is it holding? how long is left? what angle?
    rain        read the rain sensor once
    watch 2     read the rain sensor every 2 seconds (Ctrl+C to stop)
    ping        check the ESP32 is answering
    quit
Motor start/stop messages from the ESP32 are printed as they happen.
"""
import argparse
import cmd
import logging
import time

from esp32_link import Esp32Error, Esp32Link


def print_event(evt: dict) -> None:
    if evt.get("event") == "motor":
        state = "RUNNING" if evt["running"] else "STOPPED"
        reasons = {"started": "started", "timer": "time is up", "stopped": "stopped by command"}
        angle = f" (servo at {evt['angle']}°)" if "angle" in evt else ""
        print(f"\n  [motor {state}] {reasons.get(evt.get('reason'), evt.get('reason'))}{angle}")
    elif evt.get("event") == "ready":
        print("\n  [ESP32 restarted]")


def describe_rain(r) -> str:
    return f"wet {r.wet_percent:5.1f}%  raw {r.raw:4d}  {'RAINING' if r.raining else 'dry'}"


class Shell(cmd.Cmd):
    prompt = "esp32> "

    def __init__(self, esp: Esp32Link):
        super().__init__()
        self.esp = esp
        self.intro = f"Connected to ESP32 on {esp.port}. Type 'help' for commands."

    # Run each command and turn errors into a one-line message instead of a crash.
    def onecmd(self, line):
        try:
            return super().onecmd(line)
        except (Esp32Error, TimeoutError, ValueError) as e:
            print(f"  error: {e}")
        except KeyboardInterrupt:
            print()

    def emptyline(self):
        pass

    def do_on(self, arg):
        """on SECONDS [ANGLE] : move the servo (to ANGLE 0-180 if given), hold SECONDS, then back to rest"""
        parts = arg.split()
        if not 1 <= len(parts) <= 2:
            print("  usage: on SECONDS [ANGLE]")
            return
        angle = int(parts[1]) if len(parts) == 2 else None
        s = self.esp.motor_on(float(parts[0]), angle)
        print(f"  servo at {s.angle}° for {s.remaining_s:.1f} s")

    def do_off(self, arg):
        """off : stop the motor now"""
        self.esp.motor_off()
        print("  motor off")

    def do_status(self, arg):
        """status : is the motor running, and how long is left"""
        s = self.esp.motor_status()
        if s.running:
            print(f"  motor RUNNING at {s.angle}°, {s.remaining_s:.1f} s left")
        else:
            print(f"  motor STOPPED (resting at {s.angle}°)")

    def do_rain(self, arg):
        """rain : read the rain sensor once"""
        print("  " + describe_rain(self.esp.read_rain()))

    def do_watch(self, arg):
        """watch [SECONDS] : read the rain sensor every SECONDS (default 1) until Ctrl+C"""
        every = float(arg) if arg.strip() else 1.0
        print("  Ctrl+C to stop")
        while True:
            r = self.esp.read_rain()
            motor = "motor ON " if self.esp.motor_running else "motor off"
            print(f"  {time.strftime('%H:%M:%S')}  {describe_rain(r)}  {motor}")
            time.sleep(every)

    def do_ping(self, arg):
        """ping : check the ESP32 is answering"""
        t = time.monotonic()
        uptime = self.esp.ping()
        print(f"  pong in {(time.monotonic() - t) * 1000:.0f} ms (ESP32 up {uptime:.0f} s)")

    def do_quit(self, arg):
        """quit : exit (a running motor keeps going until its timer ends)"""
        return True

    do_exit = do_quit

    def do_EOF(self, arg):
        print()
        return True


def main():
    ap = argparse.ArgumentParser(description="Control the ESP32 motor/rain controller")
    ap.add_argument("--port", help="serial port, e.g. COM5 or /dev/ttyUSB0 (default: auto-detect)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING)

    with Esp32Link(a.port, a.baud, on_event=print_event) as esp:
        Shell(esp).cmdloop()


if __name__ == "__main__":
    main()
