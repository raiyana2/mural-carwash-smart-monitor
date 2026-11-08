/*
  test_bay.ino — minimal ESP32 bay firmware for testing the MQTT command path.

  It speaks the same contract as the backend (see backend/README.md) and uses
  the on-board LED as a stand-in for the wash relays:
    start        -> LED on   (mode = washing)
    stop / estop -> LED off
    pause        -> LED blinks
    set_output   -> {"output":"led","state":true/false}

  Board:      "ESP32 Dev Module" (Arduino IDE: install "esp32 by Espressif Systems")
  Libraries:  PubSubClient (Nick O'Leary), ArduinoJson (Benoit Blanchon, v7)
*/
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ---------- EDIT THESE ----------
const char* WIFI_SSID     = "your-wifi";
const char* WIFI_PASSWORD = "your-password";
const char* MQTT_HOST     = "192.168.1.42";  // IP of the machine running Mosquitto
const int   MQTT_PORT     = 1883;
const char* BAY_ID        = "bay1";
// --------------------------------

#ifndef LED_BUILTIN
#define LED_BUILTIN 2  // most ESP32 DevKit boards
#endif

WiFiClient wifi;
PubSubClient mqtt(wifi);

String base, tCmd, tAck, tStatus, tState;
const char* T_ALL = "carwash/bays/all/cmd";

String mode = "idle";      // idle | washing | paused
String program = "";
bool estop = false;
bool ledOn = false;
unsigned long lastBlink = 0;

void setLed(bool on) {
  ledOn = on;
  digitalWrite(LED_BUILTIN, on ? HIGH : LOW);
}

void allOff() {           // fail-safe: everything off
  setLed(false);
  mode = "idle";
  program = "";
}

void fillState(JsonObject s) {
  s["mode"] = mode;
  if (program.length()) s["program"] = program; else s["program"] = nullptr;
  s["estop"] = estop;
  s["outputs"]["led"] = ledOn;
  s["rssi"] = WiFi.RSSI();
}

void publishState() {
  JsonDocument doc;
  fillState(doc.to<JsonObject>());
  char buf[256];
  serializeJson(doc, buf);
  mqtt.publish(tState.c_str(), buf);
}

void onMessage(char* topic, byte* payload, unsigned int len) {
  JsonDocument cmd;
  if (deserializeJson(cmd, payload, len)) {
    Serial.println("bad JSON, ignored");
    return;
  }
  const char* id = cmd["id"] | "";
  String action = cmd["action"] | "";
  JsonObject params = cmd["params"];

  bool ok = true;
  String err;

  if (estop && action != "reset" && action != "ping" && action != "emergency_stop") {
    ok = false; err = "e-stop latched; send reset first";
  } else if (action == "start") {
    if (mode != "idle") { ok = false; err = "bay busy (" + mode + ")"; }
    else { mode = "washing"; program = params["program"] | "basic"; setLed(true); }
  } else if (action == "stop") {
    allOff();
  } else if (action == "pause") {
    if (mode == "washing") mode = "paused";
    else { ok = false; err = "not washing"; }
  } else if (action == "resume") {
    if (mode == "paused") { mode = "washing"; setLed(true); }
    else { ok = false; err = "not paused"; }
  } else if (action == "emergency_stop") {
    allOff(); estop = true;
  } else if (action == "reset") {
    estop = false;
  } else if (action == "set_output") {
    String out = params["output"] | "";
    if (out == "led") setLed(params["state"] | false);
    else { ok = false; err = "unknown output " + out; }
  } else if (action == "ping") {
    // nothing to do, just ack
  } else {
    ok = false; err = "unknown action " + action;
  }

  Serial.printf("[%s] %s -> %s\n", BAY_ID, action.c_str(), ok ? "ok" : err.c_str());

  JsonDocument ack;
  ack["id"] = id;
  ack["ok"] = ok;
  if (ok) ack["error"] = nullptr; else ack["error"] = err;
  fillState(ack["result"].to<JsonObject>());
  char buf[384];
  serializeJson(ack, buf);
  mqtt.publish(tAck.c_str(), buf);
  publishState();
}

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("WiFi: connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nWiFi: connected, IP %s\n", WiFi.localIP().toString().c_str());
}

void connectMqtt() {
  while (!mqtt.connected()) {
    Serial.printf("MQTT: connecting to %s:%d ... ", MQTT_HOST, MQTT_PORT);
    String clientId = String("esp32-") + BAY_ID;
    // Last Will: broker publishes "offline" (retained) if we vanish
    if (mqtt.connect(clientId.c_str(), tStatus.c_str(), 1, true, "offline")) {
      Serial.println("ok");
      mqtt.subscribe(tCmd.c_str(), 1);
      mqtt.subscribe(T_ALL, 1);
      mqtt.publish(tStatus.c_str(), "online", true);
      publishState();
    } else {
      Serial.printf("failed (rc=%d), retrying in 2s\n", mqtt.state());
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
  allOff();

  base    = String("carwash/bays/") + BAY_ID;
  tCmd    = base + "/cmd";
  tAck    = base + "/ack";
  tStatus = base + "/status";
  tState  = base + "/state";

  connectWifi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(512);   // default 256 is too small for acks with state
  mqtt.setCallback(onMessage);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED || !mqtt.connected()) {
    allOff();                // lost the controller -> stop the bay
    connectWifi();
    connectMqtt();
  }
  mqtt.loop();

  if (mode == "paused" && millis() - lastBlink > 400) {   // blink while paused
    lastBlink = millis();
    setLed(!ledOn);
  }
}
