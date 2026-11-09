import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import serial

from carwash.hardware import esp32_link
from carwash.hardware.esp32_link import Esp32ConnectError


class PortTests(unittest.TestCase):
    def test_ports_endpoint(self):
        fake = SimpleNamespace(device="COM7", description="USB-Enhanced-SERIAL CH9102 (COM7)", vid=0x1A86, pid=0x55D4)
        other = SimpleNamespace(device="COM1", description="Communications Port (COM1)", vid=None, pid=None)
        with patch("carwash.hardware.esp32_link.list_ports.comports", return_value=[other, fake]):
            ports = esp32_link.list_serial_ports()
            self.assertEqual([p["device"] for p in ports], ["COM7", "COM1"])
            self.assertTrue(ports[0]["likely_esp32"])
            self.assertEqual(esp32_link.find_esp32_port(), "COM7")
        with patch("carwash.hardware.esp32_link.list_ports.comports", return_value=[other]):
            with self.assertRaises(Esp32ConnectError) as raised:
                esp32_link.find_esp32_port()
            self.assertEqual(raised.exception.kind, "not_found")
            self.assertIn("COM1", str(raised.exception))

    def test_open_errors_are_explained(self):
        explain = esp32_link._explain_open_error
        with patch("carwash.hardware.esp32_link.list_ports.comports", return_value=[]):
            busy = explain("COM5", serial.SerialException("could not open port 'COM5': PermissionError(13, 'Access is denied.', None, 5)"))
            self.assertEqual(busy.kind, "busy")
            self.assertIn("serial monitor", str(busy))
            missing = explain("COM9", serial.SerialException("could not open port 'COM9': FileNotFoundError(2, 'The system cannot find the file specified.', None, 2)"))
            self.assertEqual(missing.kind, "missing_port")
            locked = explain("/dev/ttyUSB0", serial.SerialException("Could not exclusively lock port /dev/ttyUSB0: [Errno 11] Resource temporarily unavailable"))
            self.assertEqual(locked.kind, "busy")


class SerialReaderTests(unittest.TestCase):
    def test_reply_split_across_reads_is_not_lost(self):
        class ChunkedSerial:
            is_open = True

            def __init__(self, chunks):
                self.chunks = list(chunks)

            @property
            def in_waiting(self):
                return len(self.chunks[0]) if self.chunks else 0

            def read(self, n):
                if not self.chunks:
                    raise serial.SerialException("done")
                return self.chunks.pop(0)

        link = esp32_link.Esp32Link("TEST")
        slot = {"done": threading.Event(), "reply": None}
        link._pending[7] = slot
        link._ser = ChunkedSerial([b'boot noise\r\n{"type":"rep', b"", b'ly","id":7,"ok":true,', b'"data":{"uptime_s":3}}\n'])
        link._read_loop()
        self.assertTrue(slot["done"].is_set())
        self.assertEqual(slot["reply"]["data"]["uptime_s"], 3)


class OldFirmwareTests(unittest.TestCase):
    def test_old_firmware_without_motor_commands_is_explained(self):
        link = esp32_link.Esp32Link("COM5")
        with patch.object(link, "_open_port"), patch("carwash.hardware.esp32_link._is_native_usb", return_value=False), \
                patch.object(link, "_read_loop"), patch.object(link, "_wait_for_answer", return_value=True), \
                patch.object(link, "motor_status", side_effect=esp32_link.Esp32Error("unknown cmd 'motor_status'")):
            with self.assertRaises(Esp32ConnectError) as raised:
                link.open()
        self.assertEqual(raised.exception.kind, "old_firmware")
        self.assertIn("firmware/motor_rain_controller", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
