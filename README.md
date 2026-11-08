# Mural Car Wash Smart Monitor

Offline Raspberry Pi + ESP32 prototype for monitoring and demonstrating an 11-bay self-serve car wash.

- Bay 1 is the real ESP32 motor/rain-sensor test bay. Existing firmware and the `Esp32Link` USB serial client are reused.
- Bays 2–11 are explicitly labelled local simulator bays.
- FastAPI serves the local operations dashboard; SQLite stores bay settings, pricing, sessions, and fault history.
- Payments and multi-bay data are simulated. The application does not connect to a real payment system.

See [Pi setup, launch, architecture, and simulator instructions](./pi/README.md) and [ESP32 wiring and firmware protocol](./esp32/README.md).
