"""Mural car wash smart monitor: the Raspberry Pi side.

Layers, from the bottom up (each one only uses the ones below it):

    config        settings shared by every layer (limits, angles, timings, paths)
    hardware      esp32_link: the USB serial client for the ESP32 firmware
    storage       database: SQLite for bays, runs and faults
    controllers   the logic: Bay 1 hardware, the Bay 2-11 simulator, and
                  MonitorController, which ties them together for the dashboard
    web           the FastAPI routes and the browser page; no logic of its own
"""
