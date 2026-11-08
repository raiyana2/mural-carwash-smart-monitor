/*
  motor_rain_controller.ino: ESP32 firmware controlled by a Raspberry Pi over USB.

  The Pi sends one JSON request per line; the ESP32 answers each one with a
  reply carrying the same "id". The ESP32 also sends "events" on its own when
  something changes (for example the motor's timer runs out).

  Requests (Pi -> ESP32):
    {"id":1, "cmd":"motor_on", "seconds":5}   run the motor for 5 seconds
    {"id":2, "cmd":"motor_off"}                stop the motor now
    {"id":3, "cmd":"motor_status"}             is the motor running?
    {"id":4, "cmd":"rain"}                     read the rain sensor
    {"id":5, "cmd":"ping"}                     check the ESP32 is alive

  Replies (ESP32 -> Pi):
    {"type":"reply", "id":1, "ok":true,  "error":null, "data":{"running":true, "remaining_s":5.0}}
    {"type":"reply", "id":4, "ok":true,  "error":null, "data":{"raw":1830, "wet_percent":55.3, "raining":true}}
    {"type":"reply", "id":9, "ok":false, "error":"motor already running", "data":{...}}

  Events (ESP32 -> Pi, unprompted):
    {"type":"event", "event":"ready"}                                    after boot
    {"type":"event", "event":"motor", "running":true,  "reason":"started"}
    {"type":"event", "event":"motor", "running":false, "reason":"timer"}     (or "stopped")

  Library: ArduinoJson v7 (Benoit Blanchon). Board: "ESP32 Dev Module".
  Pins are set in config.h.
*/
#include <ArduinoJson.h>
#include "config.h"

// ---------------- state ----------------
bool motorRunning = false;
unsigned long motorStartedAt = 0;   // millis() when the motor was turned on
unsigned long motorRunMs = 0;       // how long it should run for

char line[256];                     // incoming line from the Pi
size_t lineLen = 0;

// ---------------- sending ----------------
void sendJson(JsonDocument& doc) {
  serializeJson(doc, Serial);
  Serial.print('\n');
}

// ---------------- motor ----------------
void writeMotorPin(bool on) {
  // Turns "on/off" into the right voltage for active-high or active-low modules.
  digitalWrite(MOTOR_PIN, (on == MOTOR_ACTIVE_HIGH) ? HIGH : LOW);
}

unsigned long motorRemainingMs() {
  if (!motorRunning) return 0;
  unsigned long elapsed = millis() - motorStartedAt;
  return elapsed >= motorRunMs ? 0 : motorRunMs - elapsed;
}

void sendMotorEvent(const char* reason) {
  JsonDocument d;
  d["type"] = "event";
  d["event"] = "motor";
  d["running"] = motorRunning;
  d["reason"] = reason;
  sendJson(d);
}

void motorStart(float seconds) {
  motorStartedAt = millis();
  motorRunMs = (unsigned long)(seconds * 1000.0f);
  motorRunning = true;
  writeMotorPin(true);
  sendMotorEvent("started");
}

void motorStop(const char* reason) {
  bool wasRunning = motorRunning;
  writeMotorPin(false);
  motorRunning = false;
  motorRunMs = 0;
  if (wasRunning) sendMotorEvent(reason);
}

void fillMotorStatus(JsonObject o) {
  o["running"] = motorRunning;
  o["remaining_s"] = motorRemainingMs() / 1000.0;
}

// ---------------- rain sensor ----------------
void fillRainReading(JsonObject o) {
  long sum = 0;
  for (int i = 0; i < RAIN_SAMPLES; i++) {
    sum += analogRead(RAIN_ANALOG_PIN);   // 0..4095 (12-bit ADC)
    delay(2);
  }
  int raw = sum / RAIN_SAMPLES;

  // On these modules the voltage DROPS as the plate gets wetter:
  // ~4095 = completely dry, lower = wetter.
  float wet = (4095 - raw) * 100.0f / 4095.0f;

  o["raw"] = raw;
  o["wet_percent"] = round(wet * 10) / 10.0;          // one decimal place
  o["raining"] = digitalRead(RAIN_DIGITAL_PIN) == LOW;
}

// ---------------- request handling ----------------
void handleRequest(const char* text, size_t len) {
  JsonDocument req;
  if (deserializeJson(req, text, len)) return;      // not JSON (e.g. boot noise): ignore

  String cmd = req["cmd"] | "";
  bool ok = true;
  String error;

  JsonDocument reply;
  reply["type"] = "reply";
  reply["id"] = req["id"];                           // echo the id so the Pi can match it
  JsonObject data = reply["data"].to<JsonObject>();

  if (cmd == "motor_on") {
    float seconds = req["seconds"] | 0.0f;
    if (seconds <= 0 || seconds > MAX_RUN_SECONDS) {
      ok = false;
      error = String("seconds must be > 0 and <= ") + MAX_RUN_SECONDS;
    } else if (motorRunning) {
      ok = false;
      error = "motor already running";
    } else {
      motorStart(seconds);
    }
    fillMotorStatus(data);

  } else if (cmd == "motor_off") {
    motorStop("stopped");
    fillMotorStatus(data);

  } else if (cmd == "motor_status") {
    fillMotorStatus(data);

  } else if (cmd == "rain") {
    fillRainReading(data);

  } else if (cmd == "ping") {
    data["uptime_s"] = millis() / 1000.0;

  } else {
    ok = false;
    error = "unknown cmd '" + cmd + "'";
  }

  reply["ok"] = ok;
  if (ok) reply["error"] = nullptr; else reply["error"] = error;
  sendJson(reply);
}

void readSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen > 0) handleRequest(line, lineLen);
      lineLen = 0;
    } else if (lineLen < sizeof(line) - 1) {
      line[lineLen++] = c;
    } else {
      lineLen = 0;                                   // too long: drop it
    }
  }
}

// ---------------- main ----------------
void setup() {
  writeMotorPin(false);          // set the "off" level before the pin becomes an output
  pinMode(MOTOR_PIN, OUTPUT);
  writeMotorPin(false);

  pinMode(RAIN_DIGITAL_PIN, INPUT);
  analogReadResolution(12);

  Serial.begin(SERIAL_BAUD);
  delay(100);

  JsonDocument ready;
  ready["type"] = "event";
  ready["event"] = "ready";
  sendJson(ready);
}

void loop() {
  readSerial();

  // Timer: stop the motor when its time is up. This runs on the ESP32 itself,
  // so the motor still stops on time even if the Pi crashes or the cable is pulled.
  if (motorRunning && motorRemainingMs() == 0) {
    motorStop("timer");
  }
}
