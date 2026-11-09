"""cli.py: interactive control of the ESP32 from the Raspberry Pi. Run from the pi folder:

    python -m tools.cli                    # auto-detect the ESP32's USB port
    python -m tools.cli --port COM5        # Windows
    python -m tools.cli --port /dev/ttyUSB0

Then type commands at the  esp32>  prompt:
    angle 90    move the servo to 90 degrees and leave it there
    on 5        timed hold: servo to its "on" angle, hold 5 seconds, then back to where it was
    on 5 90     same, but to 90 degrees
    off         end a timed hold now (back to where it was)
    status      is it holding? how long is left? what angle?
    rain        read the rain sensor once
    distance    read the ultrasonic sensor once
    button      is the button held down right now?
    lcd 0 Hello put "Hello" on LCD row 0 (or 1)
    lcdclear    blank the LCD
    backlight on|off
    watch 2     read the sensors every 2 seconds (Ctrl+C to stop)
    ping        check the ESP32 is answering and list its features
    quit
Motor start/stop messages and button presses from the ESP32 are printed as they happen.
"""
import argparse
import cmd
import logging
import time

from carwash.hardware.esp32_link import Esp32ConnectError, Esp32Error, Esp32Link


def print_event(evt: dict) -> None:
    if evt.get("event") == "motor":
        state = "RUNNING" if evt["running"] else "STOPPED"
        reasons = {"started": "started", "timer": "time is up", "stopped": "stopped by command"}
        angle = f" (servo at {evt['angle']}°)" if "angle" in evt else ""
        print(f"\n  [motor {state}] {reasons.get(evt.get('reason'), evt.get('reason'))}{angle}")
    elif evt.get("event") == "button":
        print("\n  [button pressed]")
    elif evt.get("event") == "ready":
        print("\n  [ESP32 restarted]")


def describe_rain(r) -> str:
    return f"wet {r.wet_percent:5.1f}%  raw {r.raw:4d}  {'RAINING' if r.raining else 'dry'}"


def describe_distance(cm) -> str:
    return "nothing in range" if cm is None else f"{cm:5.1f} cm"


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
        """on SECONDS [ANGLE] : move the servo (to ANGLE 0-180 if given), hold SECONDS, then back to where it was"""
        parts = arg.split()
        if not 1 <= len(parts) <= 2:
            print("  usage: on SECONDS [ANGLE]")
            return
        angle = int(parts[1]) if len(parts) == 2 else None
        s = self.esp.motor_on(float(parts[0]), angle)
        print(f"  servo at {s.angle}° for {s.remaining_s:.1f} s")

    def do_angle(self, arg):
        """angle DEGREES : move the servo to DEGREES (0-180) and leave it there"""
        if not arg.strip():
            print("  usage: angle DEGREES")
            return
        s = self.esp.servo_set(int(arg))
        print(f"  servo at {s.angle}°")

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
            print(f"  motor STOPPED (servo at {s.angle}°)")

    def do_rain(self, arg):
        """rain : read the rain sensor once"""
        print("  " + describe_rain(self.esp.read_rain()))

    def do_distance(self, arg):
        """distance : read the ultrasonic sensor once"""
        print("  " + describe_distance(self.esp.read_distance()))

    def do_button(self, arg):
        """button : is the button held down right now? (presses are also printed as they happen)"""
        print("  pressed" if self.esp.read_button() else "  not pressed")

    def do_lcd(self, arg):
        """lcd ROW TEXT : show TEXT on LCD row 0 or 1 (16 characters)"""
        row, _, text = arg.partition(" ")
        if row not in ("0", "1"):
            print("  usage: lcd ROW TEXT   (ROW is 0 or 1)")
            return
        print(f"  LCD row {row}: {self.esp.lcd_text(int(row), text)!r}")

    def do_lcdclear(self, arg):
        """lcdclear : blank the LCD"""
        self.esp.lcd_clear()
        print("  LCD cleared")

    def do_backlight(self, arg):
        """backlight on|off : turn the LCD backlight on or off"""
        if arg.strip() not in ("on", "off"):
            print("  usage: backlight on|off")
            return
        self.esp.lcd_backlight(arg.strip() == "on")
        print(f"  backlight {arg.strip()}")

    def do_watch(self, arg):
        """watch [SECONDS] : read the sensors every SECONDS (default 1) until Ctrl+C"""
        every = float(arg) if arg.strip() else 1.0
        print("  Ctrl+C to stop")
        while True:
            r = self.esp.read_rain()
            distance = f"  {describe_distance(self.esp.read_distance())}" if self.esp.has("distance") else ""
            motor = "motor ON " if self.esp.motor_running else "motor off"
            print(f"  {time.strftime('%H:%M:%S')}  {describe_rain(r)}{distance}  {motor}")
            time.sleep(every)

    def do_ping(self, arg):
        """ping : check the ESP32 is answering, and list what its firmware has"""
        t = time.monotonic()
        uptime = self.esp.ping()
        print(f"  pong in {(time.monotonic() - t) * 1000:.0f} ms (ESP32 up {uptime:.0f} s)")
        print(f"  firmware features: {', '.join(sorted(self.esp.features))}")

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

    try:
        esp = Esp32Link(a.port, a.baud, on_event=print_event).open()
    except Esp32ConnectError as e:
        raise SystemExit(f"Can't connect: {e}")
    try:
        Shell(esp).cmdloop()
    finally:
        esp.close()


if __name__ == "__main__":
    main()
