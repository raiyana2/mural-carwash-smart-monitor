/*
  main.cpp: ESP32 firmware controlled by a Raspberry Pi over USB serial.
  Drives a positional servo and reads a rain sensor. Pins and limits are in
  ../config.h (firmware/motor_rain_controller/config.h).

  This is plain C++, built as-is by both tools:
    PlatformIO:  open the repo folder and press Upload (platformio.ini points at this src folder).
    Arduino IDE: open ../motor_rain_controller.ino; the IDE also compiles everything in src/.

  Every message is one JSON object per line. The Pi sends a request with an
  "id"; the ESP32 answers with a reply carrying the same id. The ESP32 also
  sends events on its own when something changes.

  Requests (Pi -> ESP32):
    {"id":1,"cmd":"servo_set","angle":90}              move to 90° and stay there
    {"id":2,"cmd":"servo_status"}                      current angle (+ timed-hold info)
    {"id":3,"cmd":"motor_on","seconds":5}              timed hold: go to SERVO_ACTIVE_ANGLE,
    {"id":4,"cmd":"motor_on","seconds":5,"angle":90}     hold N seconds, then back to the angle
                                                       it was at before the hold started
    {"id":5,"cmd":"motor_off"}                         end a timed hold now (back to that angle)
    {"id":6,"cmd":"motor_status"}                      is a timed hold running? time left?
    {"id":7,"cmd":"rain"}                              read the rain sensor
    {"id":8,"cmd":"ping"}                              check the ESP32 is alive

  Replies (ESP32 -> Pi):
    {"type":"reply","id":1,"ok":true,"error":null,"data":{"angle":90,"running":false,"remaining_s":0}}
    {"type":"reply","id":7,"ok":true,"error":null,"data":{"raw":1830,"wet_percent":55.3,"raining":true}}

  Events (ESP32 -> Pi, unprompted):
    {"type":"event","event":"ready"}                                      after boot
    {"type":"event","event":"servo","angle":90,"running":false}           every time the angle changes
    {"type":"event","event":"motor","running":true,"reason":"started","angle":180}
    {"type":"event","event":"motor","running":false,"reason":"timer","angle":0}   (or "stopped")

  servo_set is refused while a timed hold is running (send motor_off first),
  so a manual move can't be undone by the hold's timer a moment later.
*/
#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include "../config.h"

// ---------------- state ----------------
Servo myServo;
int currentAngle = SERVO_REST_ANGLE;

bool holdRunning = false;           // a timed hold (motor_on) is in progress
unsigned long holdStartedAt = 0;    // millis() when it started
unsigned long holdMs = 0;           // how long it lasts
int holdReturnAngle = SERVO_REST_ANGLE;  // angle to go back to when the hold ends

char line[256];                     // incoming line from the Pi
size_t lineLen = 0;

// ---------------- sending ----------------
void sendJson(JsonDocument& doc) {
  serializeJson(doc, Serial);
  Serial.print('\n');
}

// ---------------- servo control ----------------
void sendServoEvent(int angle) {
  JsonDocument d;
  d["type"] = "event";
  d["event"] = "servo";
  d["angle"] = angle;
  d["running"] = holdRunning;        // true when this move is part of a timed hold
  sendJson(d);
}

void setServoAngle(int angle) {
  angle = constrain(angle, 0, 180);  // safe servo limits
  currentAngle = angle;
  myServo.write(currentAngle);       // sends the 50 Hz pulses for this angle
  sendServoEvent(currentAngle);
}

// ---------------- timed hold ----------------
unsigned long holdRemainingMs() {
  if (!holdRunning) return 0;
  unsigned long elapsed = millis() - holdStartedAt;
  return elapsed >= holdMs ? 0 : holdMs - elapsed;
}

void sendMotorEvent(const char* reason) {
  JsonDocument d;
  d["type"] = "event";
  d["event"] = "motor";
  d["running"] = holdRunning;
  d["reason"] = reason;
  d["angle"] = currentAngle;
  sendJson(d);
}

void holdStart(float seconds, int angle) {
  holdStartedAt = millis();
  holdMs = (unsigned long)(seconds * 1000.0f);
  holdRunning = true;
  holdReturnAngle = currentAngle;    // remember where we started from
  setServoAngle(angle);
  sendMotorEvent("started");
}

void holdStop(const char* reason) {
  if (!holdRunning) return;          // nothing to stop: leave the servo where it is
  holdRunning = false;
  holdMs = 0;
  setServoAngle(holdReturnAngle);    // back to where it was before the hold
  sendMotorEvent(reason);
}

// ---------------- status ----------------
void fillServoStatus(JsonObject o) {
  o["angle"] = currentAngle;
  o["running"] = holdRunning;
  o["remaining_s"] = holdRemainingMs() / 1000.0;
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
  if (deserializeJson(req, text, len)) return;       // not JSON: ignore

  String cmd = req["cmd"] | "";
  bool ok = true;
  String error;

  JsonDocument reply;
  reply["type"] = "reply";
  reply["id"] = req["id"];                           // echo the id so the Pi can match it
  JsonObject data = reply["data"].to<JsonObject>();

  if (cmd == "servo_set") {
    int angle = req["angle"] | -1;
    if (angle < 0 || angle > 180) {
      ok = false;
      error = "angle must be between 0 and 180";
    } else if (holdRunning) {
      ok = false;
      error = "timed hold running; send motor_off first";
    } else {
      setServoAngle(angle);
    }
    fillServoStatus(data);

  } else if (cmd == "servo_status" || cmd == "motor_status") {
    fillServoStatus(data);

  } else if (cmd == "motor_on") {
    float seconds = req["seconds"] | 0.0f;
    int angle = req["angle"] | SERVO_ACTIVE_ANGLE;   // optional; default from config.h
    if (seconds <= 0 || seconds > MAX_RUN_SECONDS) {
      ok = false;
      error = String("seconds must be > 0 and <= ") + MAX_RUN_SECONDS;
    } else if (angle < 0 || angle > 180) {
      ok = false;
      error = "angle must be between 0 and 180";
    } else if (holdRunning) {
      ok = false;
      error = "motor already running";
    } else {
      holdStart(seconds, angle);
    }
    fillServoStatus(data);

  } else if (cmd == "motor_off") {
    holdStop("stopped");
    fillServoStatus(data);

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
  delay(100);

  myServo.setPeriodHertz(50);                        // standard servo signal: 50 pulses per second
  myServo.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
  setServoAngle(SERVO_REST_ANGLE);                   // note: the servo moves here at power-up

  pinMode(RAIN_DIGITAL_PIN, INPUT);
  analogReadResolution(12);

  JsonDocument ready;
  ready["type"] = "event";
  ready["event"] = "ready";
  sendJson(ready);
}

void loop() {
  readSerial();

  // Timer: end the hold when its time is up. This runs on the ESP32 itself,
  // so it still happens even if the Pi crashes or the cable is pulled.
  if (holdRunning && holdRemainingMs() == 0) {
    holdStop("timer");
  }
}
