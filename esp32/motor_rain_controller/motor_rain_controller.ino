/*
  motor_rain_controller.ino: ESP32 firmware controlled by a Raspberry Pi over USB.
  Drives a POSITIONAL servo and reads a rain sensor.

  "Motor on" for a positional servo means: move to an angle, hold it for N
  seconds, then go back to the rest angle. The protocol keeps the name "motor"
  so the Pi code doesn't change.

  Requests (Pi -> ESP32), one JSON object per line:
    {"id":1, "cmd":"motor_on", "seconds":5}               move to SERVO_ACTIVE_ANGLE, hold 5 s
    {"id":2, "cmd":"motor_on", "seconds":5, "angle":90}   move to 90°, hold 5 s
    {"id":3, "cmd":"motor_off"}                            go back to rest now
    {"id":4, "cmd":"motor_status"}                         running? time left? angle?
    {"id":5, "cmd":"rain"}                                 read the rain sensor
    {"id":6, "cmd":"ping"}                                 check the ESP32 is alive

  Replies (ESP32 -> Pi):
    {"type":"reply","id":1,"ok":true,"error":null,"data":{"running":true,"remaining_s":5.0,"angle":180}}
    {"type":"reply","id":5,"ok":true,"error":null,"data":{"raw":1830,"wet_percent":55.3,"raining":true}}

  Events (ESP32 -> Pi, unprompted):
    {"type":"event","event":"ready"}
    {"type":"event","event":"motor","running":true, "reason":"started","angle":180}
    {"type":"event","event":"motor","running":false,"reason":"timer","angle":0}   (or "stopped")

  Libraries: ArduinoJson v7 (Benoit Blanchon), ESP32Servo (Kevin Harrington).
  Board: "ESP32 Dev Module". Pins and angles are set in config.h.
*/
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include "config.h"

// ---------------- state ----------------
Servo servo;
int servoAngle = SERVO_REST_ANGLE;  // last angle we told the servo to go to

bool motorRunning = false;          // true while holding the active angle
unsigned long motorStartedAt = 0;   // millis() when the hold started
unsigned long motorRunMs = 0;       // how long to hold

char line[256];                     // incoming line from the Pi
size_t lineLen = 0;

// ---------------- sending ----------------
void sendJson(JsonDocument& doc) {
  serializeJson(doc, Serial);
  Serial.print('\n');
}

// ---------------- servo ----------------
void moveServo(int angle) {
  // servo.write() sends the 50 Hz pulses for this angle; the servo then
  // turns there at its own speed and holds the position.
  servoAngle = angle;
  servo.write(angle);
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
  d["angle"] = servoAngle;
  sendJson(d);
}

void motorStart(float seconds, int angle) {
  motorStartedAt = millis();
  motorRunMs = (unsigned long)(seconds * 1000.0f);
  motorRunning = true;
  moveServo(angle);
  sendMotorEvent("started");
}

void motorStop(const char* reason) {
  bool wasRunning = motorRunning;
  motorRunning = false;
  motorRunMs = 0;
  moveServo(SERVO_REST_ANGLE);
  if (wasRunning) sendMotorEvent(reason);
}

void fillMotorStatus(JsonObject o) {
  o["running"] = motorRunning;
  o["remaining_s"] = motorRemainingMs() / 1000.0;
  o["angle"] = servoAngle;
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
    int angle = req["angle"] | SERVO_ACTIVE_ANGLE;   // optional; default from config.h
    if (seconds <= 0 || seconds > MAX_RUN_SECONDS) {
      ok = false;
      error = String("seconds must be > 0 and <= ") + MAX_RUN_SECONDS;
    } else if (angle < 0 || angle > 180) {
      ok = false;
      error = "angle must be 0-180";
    } else if (motorRunning) {
      ok = false;
      error = "motor already running";
    } else {
      motorStart(seconds, angle);
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
  Serial.begin(SERIAL_BAUD);

  servo.setPeriodHertz(50);                          // standard servo signal: 50 pulses per second
  servo.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
  moveServo(SERVO_REST_ANGLE);                       // note: the servo moves to rest at power-up

  pinMode(RAIN_DIGITAL_PIN, INPUT);
  analogReadResolution(12);

  delay(100);
  JsonDocument ready;
  ready["type"] = "event";
  ready["event"] = "ready";
  sendJson(ready);
}

void loop() {
  readSerial();

  // Timer: go back to rest when the hold time is up. This runs on the ESP32
  // itself, so it still happens even if the Pi crashes or the cable is pulled.
  if (motorRunning && motorRemainingMs() == 0) {
    motorStop("timer");
  }
}
