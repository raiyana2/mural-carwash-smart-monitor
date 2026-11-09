/*
  main.cpp: ESP32 firmware for Bay 1, controlled by a Raspberry Pi over USB serial.
  Drives a positional servo (the "motor"), and reads a rain sensor, an ultrasonic
  distance sensor and a push button. Shows status on a 16x2 I2C LCD. Pins and
  limits are in ../config.h (firmware/motor_rain_controller/config.h).

  This is plain C++, built as-is by both tools:
    PlatformIO:  open the repo folder and press Upload (platformio.ini points at this src folder).
    Arduino IDE: open ../motor_rain_controller.ino; the IDE also compiles everything in src/.
                 Libraries: ArduinoJson 7, ESP32Servo 3, LiquidCrystal I2C (Marco Schwartz).

  Every message is one JSON object per line. The Pi sends a request with an
  "id"; the ESP32 answers with a reply carrying the same id. The ESP32 also
  sends events on its own when something changes.

  Requests (Pi -> ESP32):
    {"id":1,"cmd":"motor_on","seconds":5}              run: turn to SERVO_ACTIVE_ANGLE (180°), hold 5 s,
    {"id":2,"cmd":"motor_on","seconds":5,"angle":90}     then turn back to the angle it started from
    {"id":2,"cmd":"motor_on","seconds":300,"label":"Wash $5.00"}   label = LCD line 1 during the run
    {"id":3,"cmd":"motor_off"}                         end a run now (turn back)
    {"id":4,"cmd":"motor_status"}                      is a run in progress? time left? angle?
    {"id":5,"cmd":"servo_set","angle":90}              move to 90° and stay there (refused during a run)
    {"id":6,"cmd":"servo_status"}                      same as motor_status
    {"id":7,"cmd":"rain"}                              read the rain sensor
    {"id":8,"cmd":"distance"}                          read the ultrasonic sensor
    {"id":9,"cmd":"button"}                            is the button held down right now?
    {"id":10,"cmd":"lcd_text","row":0,"text":"Hi"}     set a line of the LCD (row 0 or 1)
    {"id":11,"cmd":"lcd_clear"}                        blank the LCD
    {"id":12,"cmd":"lcd_backlight","on":false}         backlight on or off
    {"id":13,"cmd":"ping"}                             alive? which features does this firmware have?

  Replies (ESP32 -> Pi):
    {"type":"reply","id":1,"ok":true,"error":null,"data":{"angle":180,"running":true,"remaining_s":5}}
    {"type":"reply","id":7,"ok":true,"error":null,"data":{"raw":1830,"wet_percent":55.3,"raining":true}}
    {"type":"reply","id":8,"ok":true,"error":null,"data":{"distance_cm":42.7}}       (-1 = no echo in range)
    {"type":"reply","id":9,"ok":true,"error":null,"data":{"pressed":false}}
    {"type":"reply","id":13,"ok":true,"error":null,"data":{"uptime_s":42.0,"firmware":"motor_rain_controller",
                                                           "version":2,"features":["motor","rain","distance","button","lcd"]}}

  Events (ESP32 -> Pi, unprompted):
    {"type":"event","event":"ready"}                                      after boot
    {"type":"event","event":"servo","angle":90,"running":false}           every time the angle changes
    {"type":"event","event":"motor","running":true,"reason":"started","angle":180}
    {"type":"event","event":"motor","running":false,"reason":"timer","angle":0}   (or "stopped")
    {"type":"event","event":"button","pressed":true}                      each time the button is pressed

  The LCD shows the text the Pi last set with lcd_text (the price, between washes).
  While a run is in progress the firmware shows the run's label (the price paid, or
  "Motor running") and counts down the time left by itself, then puts the Pi's text
  back when the run ends, so the countdown keeps going even if the Pi or the cable drops.
*/
#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include "../config.h"

#define FIRMWARE_VERSION 2

// ---------------- state ----------------
Servo myServo;
int currentAngle = SERVO_REST_ANGLE;

bool holdRunning = false;           // a timed run (motor_on) is in progress
unsigned long holdStartedAt = 0;    // millis() when it started
unsigned long holdMs = 0;           // how long it lasts
int holdReturnAngle = SERVO_REST_ANGLE;  // angle to go back to when the run ends

LiquidCrystal_I2C lcd(LCD_ADDRESS, LCD_COLS, LCD_ROWS);
char lcdIdleText[LCD_ROWS][LCD_COLS + 1] = {"ESP32 Ready", ""};  // what the Pi asked to show
char lcdRunLabel[LCD_COLS + 1] = "Motor running";                  // line 1 during a run
long lcdShownSeconds = -1;          // countdown value currently on screen during a run

int buttonStable = HIGH;            // debounced button level (HIGH = released, pull-up)
int buttonLastRead = HIGH;
unsigned long buttonChangedAt = 0;

char line[256];                     // incoming line from the Pi
size_t lineLen = 0;

// ---------------- sending ----------------
void sendJson(JsonDocument& doc) {
  serializeJson(doc, Serial);
  Serial.print('\n');
}

// ---------------- LCD ----------------
// Writes one full row: the text is cut to fit and padded with spaces, so no old
// characters are left behind.
void lcdWriteRow(int row, const char* text) {
  char padded[LCD_COLS + 1];
  size_t n = strlen(text);
  for (int i = 0; i < LCD_COLS; i++) padded[i] = (size_t)i < n ? text[i] : ' ';
  padded[LCD_COLS] = '\0';
  lcd.setCursor(0, row);
  lcd.print(padded);
}

void lcdShowIdle() {
  for (int row = 0; row < LCD_ROWS; row++) lcdWriteRow(row, lcdIdleText[row]);
}

void lcdShowCountdown(unsigned long remainingMs) {
  long seconds = (long)((remainingMs + 999) / 1000);   // round up: shows 0:01 until it ends
  if (seconds == lcdShownSeconds) return;
  lcdShownSeconds = seconds;
  char text[24];                                       // lcdWriteRow cuts it to the screen width
  snprintf(text, sizeof(text), "%d:%02d left", (int)(seconds / 60), (int)(seconds % 60));
  lcdWriteRow(1, text);
}

// ---------------- servo control ----------------
void sendServoEvent(int angle) {
  JsonDocument d;
  d["type"] = "event";
  d["event"] = "servo";
  d["angle"] = angle;
  d["running"] = holdRunning;        // true when this move is part of a timed run
  sendJson(d);
}

void setServoAngle(int angle) {
  angle = constrain(angle, 0, 180);  // safe servo limits
  currentAngle = angle;
  myServo.write(currentAngle);       // sends the 50 Hz pulses for this angle
  sendServoEvent(currentAngle);
}

// ---------------- timed run ----------------
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

void holdStart(float seconds, int angle, const char* label) {
  holdStartedAt = millis();
  holdMs = (unsigned long)(seconds * 1000.0f);
  holdRunning = true;
  holdReturnAngle = currentAngle;    // remember where we started from
  setServoAngle(angle);
  sendMotorEvent("started");
  strncpy(lcdRunLabel, (label && label[0]) ? label : "Motor running", LCD_COLS);
  lcdRunLabel[LCD_COLS] = '\0';
  lcdWriteRow(0, lcdRunLabel);
  lcdShownSeconds = -1;
  lcdShowCountdown(holdMs);
}

void holdStop(const char* reason) {
  if (!holdRunning) return;          // nothing to stop: leave the servo where it is
  holdRunning = false;
  holdMs = 0;
  setServoAngle(holdReturnAngle);    // back to where it was before the run
  sendMotorEvent(reason);
  lcdShowIdle();
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

// ---------------- ultrasonic sensor ----------------
void fillDistanceReading(JsonObject o) {
  digitalWrite(ULTRASONIC_TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(ULTRASONIC_TRIG_PIN, HIGH);           // a 10 µs pulse starts a measurement
  delayMicroseconds(10);
  digitalWrite(ULTRASONIC_TRIG_PIN, LOW);

  // ECHO stays HIGH for as long as the sound took to go out and come back.
  unsigned long duration = pulseIn(ULTRASONIC_ECHO_PIN, HIGH, ULTRASONIC_TIMEOUT_US);

  float distanceCm = -1.0f;                          // -1 = no echo (nothing in range)
  if (duration > 0) {
    distanceCm = (duration * 0.0343f) / 2.0f;        // speed of sound: 0.0343 cm/µs, there and back
    distanceCm = round(distanceCm * 10) / 10.0f;
  }
  o["distance_cm"] = distanceCm;
}

// ---------------- push button ----------------
// Called every loop: reports each press once, after the contacts stop bouncing.
void pollButton() {
  int level = digitalRead(BUTTON_PIN);
  unsigned long now = millis();
  if (level != buttonLastRead) {
    buttonLastRead = level;
    buttonChangedAt = now;
  }
  if (level != buttonStable && now - buttonChangedAt >= BUTTON_DEBOUNCE_MS) {
    buttonStable = level;
    if (buttonStable == LOW) {                       // LOW = pressed (pull-up)
      JsonDocument d;
      d["type"] = "event";
      d["event"] = "button";
      d["pressed"] = true;
      sendJson(d);
    }
  }
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
      holdStart(seconds, angle, req["label"] | "");
    }
    fillServoStatus(data);

  } else if (cmd == "motor_off") {
    holdStop("stopped");
    fillServoStatus(data);

  } else if (cmd == "rain") {
    fillRainReading(data);

  } else if (cmd == "distance") {
    fillDistanceReading(data);

  } else if (cmd == "button") {
    data["pressed"] = buttonStable == LOW;

  } else if (cmd == "lcd_text") {
    int row = req["row"] | 0;
    const char* msg = req["text"] | "";
    if (row < 0 || row >= LCD_ROWS) {
      ok = false;
      error = "row must be 0 or 1";
    } else {
      strncpy(lcdIdleText[row], msg, LCD_COLS);
      lcdIdleText[row][LCD_COLS] = '\0';
      // During a run the countdown owns the screen; the text appears when the run ends.
      if (!holdRunning) lcdWriteRow(row, lcdIdleText[row]);
      data["row"] = row;
      data["text"] = lcdIdleText[row];
    }

  } else if (cmd == "lcd_clear") {
    for (int row = 0; row < LCD_ROWS; row++) lcdIdleText[row][0] = '\0';
    if (!holdRunning) lcd.clear();

  } else if (cmd == "lcd_backlight") {
    bool on = req["on"] | true;
    if (on) lcd.backlight(); else lcd.noBacklight();
    data["backlight"] = on;

  } else if (cmd == "ping") {
    data["uptime_s"] = millis() / 1000.0;
    data["firmware"] = "motor_rain_controller";
    data["version"] = FIRMWARE_VERSION;
    JsonArray features = data["features"].to<JsonArray>();
    features.add("motor");
    features.add("rain");
    features.add("distance");
    features.add("button");
    features.add("lcd");

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

  Wire.begin(LCD_SDA, LCD_SCL);                      // I2C bus for the LCD
  lcd.init();
  lcd.backlight();
  lcdShowIdle();

  myServo.setPeriodHertz(50);                        // standard servo signal: 50 pulses per second
  myServo.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
  setServoAngle(SERVO_REST_ANGLE);                   // note: the servo moves here at power-up

  pinMode(RAIN_DIGITAL_PIN, INPUT);
  pinMode(ULTRASONIC_TRIG_PIN, OUTPUT);
  digitalWrite(ULTRASONIC_TRIG_PIN, LOW);
  pinMode(ULTRASONIC_ECHO_PIN, INPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  analogReadResolution(12);

  JsonDocument ready;
  ready["type"] = "event";
  ready["event"] = "ready";
  sendJson(ready);
}

void loop() {
  readSerial();
  pollButton();

  if (holdRunning) {
    unsigned long remaining = holdRemainingMs();
    // Timer: end the run when its time is up. This runs on the ESP32 itself,
    // so it still happens even if the Pi crashes or the cable is pulled.
    if (remaining == 0) holdStop("timer");
    else lcdShowCountdown(remaining);
  }
}
