#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include "config.h"

// ---------------- state ----------------
Servo myServo;
int currentAngle = 0;

char line[256];                     
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
    sum += analogRead(RAIN_ANALOG_PIN);   
    delay(2);
  }
  int raw = sum / RAIN_SAMPLES;
  float wet = (4095 - raw) * 100.0f / 4095.0f;

  o["raw"] = raw;
  o["wet_percent"] = round(wet * 10) / 10.0;          
  o["raining"] = digitalRead(RAIN_DIGITAL_PIN) == LOW;
}

// ---------------- ultrasonic sensor ----------------
void fillDistanceReading(JsonObject o) {
  digitalWrite(ULTRASONIC_TRIG_PIN, LOW);
  delayMicroseconds(2);
  
  digitalWrite(ULTRASONIC_TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(ULTRASONIC_TRIG_PIN, LOW);

  long duration = pulseIn(ULTRASONIC_ECHO_PIN, HIGH, 30000);
  
  float distance_cm = -1.0;
  if (duration > 0) {
    distance_cm = (duration * 0.0343f) / 2.0f;
    distance_cm = round(distance_cm * 10) / 10.0; 
  }

  o["distance_cm"] = distance_cm;
}

// ---------------- request handling ----------------
void handleRequest(const char* text, size_t len) {
  JsonDocument req;
  if (deserializeJson(req, text, len)) return;       

  String cmd = req["cmd"] | "";
  bool ok = true;
  String error;

  JsonDocument reply;
  reply["type"] = "reply";
  reply["id"] = req["id"];                           
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

  } else if (cmd == "distance") {
    fillDistanceReading(data);

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
      lineLen = 0;                                   
    }
  }
}

// ---------------- main ----------------
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(100);

  myServo.attach(MOTOR_PIN);
  setServoAngle(0); 

  pinMode(RAIN_DIGITAL_PIN, INPUT);
  pinMode(ULTRASONIC_TRIG_PIN, OUTPUT);
  pinMode(ULTRASONIC_ECHO_PIN, INPUT);
  
  analogReadResolution(12);

  JsonDocument ready;
  ready["type"] = "event";
  ready["event"] = "ready";
  sendJson(ready);
}

void loop() {
  readSerial();
}