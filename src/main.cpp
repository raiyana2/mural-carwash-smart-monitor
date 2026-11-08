#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include "config.h"

// ---------------- state ----------------
Servo myServo;
int currentAngle = 0;

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
  sendJson(d);
}

void setServoAngle(int angle) {
  // Constrain to safe servo limits (0 to 180 degrees)
  angle = constrain(angle, 0, 180);
  currentAngle = angle;
  myServo.write(currentAngle);
  sendServoEvent(currentAngle);
}

void fillServoStatus(JsonObject o) {
  o["angle"] = currentAngle;
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
    } else {
      setServoAngle(angle);
    }
    fillServoStatus(data);

  } else if (cmd == "servo_status") {
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

  // Attach the MS18 servo to the signal pin defined in config.h (MOTOR_PIN)
  myServo.attach(MOTOR_PIN);
  setServoAngle(0); // Initialize to 0 degrees on boot

  pinMode(RAIN_DIGITAL_PIN, INPUT);
  analogReadResolution(12);

  JsonDocument ready;
  ready["type"] = "event";
  ready["event"] = "ready";
  sendJson(ready);
}

void loop() {
  readSerial();
}
