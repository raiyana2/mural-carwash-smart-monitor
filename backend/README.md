# Car Wash Control API (backend)

This is a FastAPI service for the Raspberry Pi. It turns REST calls into MQTT commands for the ESP32 bay controllers, waits for each bay to acknowledge, and tracks which bays are online.

```
Dashboard ──HTTP──> FastAPI (Pi) ──MQTT──> Mosquitto (Pi) ──MQTT──> ESP32 bays
                          ^                                            │
                          └──────────── acks / status / state ─────────┘
```

## Run it

```bash
cd backend
python -m venv .venv && .venv\Scripts\activate      # Pi/Linux: source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d                                 # or: install mosquitto locally
copy .env.example .env                               # Pi/Linux: cp
uvicorn app.main:app --host 0.0.0.0 --port 8000
python tools/fake_esp32.py --bay bay1                # emulated bay, in another terminal
```

Interactive docs are at http://localhost:8000/docs.

Tests: `pytest`. They need a broker on localhost:1883 and start a fake bay automatically.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET  | `/health` | Shows whether the API is connected to the broker |
| GET  | `/bays` | Lists every bay seen so far, with online status and last state |
| GET  | `/bays/{bay_id}` | Shows one bay |
| POST | `/bays/{bay_id}/commands` | Sends a command to one bay |
| POST | `/bays/all/emergency-stop` | Broadcasts an emergency stop to every bay |
| GET  | `/commands/{id}` | Shows a command's status: `sent` / `acked` / `rejected` / `timeout` |

```bash
curl -X POST localhost:8000/bays/bay1/commands -H "Content-Type: application/json" \
     -d '{"action":"start","params":{"program":"deluxe"}}'
```

Actions: `start` (needs `params.program`), `stop`, `pause`, `resume`, `emergency_stop`, `reset`, `set_output` (needs `params.output` and a boolean `params.state`), `ping`.

Responses: **200** the bay acked; **409** the bay refused (for example, it's busy or the e-stop is latched); **504** no ack before the timeout; **503** the broker is down; **422** the request is invalid. With `"wait_for_ack": false` the call returns right away, and you poll `/commands/{id}` for the result.

If `CARWASH_API_KEY` is set, every request except `/health` must send the header `X-API-Key: <key>`.

## MQTT contract (what the ESP32 firmware must do)

The topic prefix defaults to `carwash`, and `<bay>` is an id like `bay1`.

| Topic | Direction | Payload |
|---|---|---|
| `carwash/bays/<bay>/cmd` | Pi → ESP32 | `{"id":"<hex>","action":"start","params":{...},"ts":"..."}` (QoS 1) |
| `carwash/bays/all/cmd` | Pi → ESP32 | Same format, sent to every bay |
| `carwash/bays/<bay>/ack` | ESP32 → Pi | `{"id":"<same id>","ok":true,"error":null,"result":{...}}` |
| `carwash/bays/<bay>/status` | ESP32 → Pi | `online` / `offline`, **retained**. Set `offline` as the Last Will. |
| `carwash/bays/<bay>/state` | ESP32 → Pi | Any JSON telemetry (optional) |

On connect, the ESP32 should:
1. Set its Last Will to `status`=`offline` (retained).
2. Subscribe to its own `cmd` topic and to `bays/all/cmd`.
3. Publish `status`=`online` (retained).

For every command, it publishes an `ack` carrying the same `id`.

ESP32 sketch outline (Arduino + PubSubClient + ArduinoJson):

```cpp
const char* BAY = "bay1";
String base = String("carwash/bays/") + BAY;

void reconnect() {
  while (!mqtt.connected()) {
    if (mqtt.connect(BAY, (base + "/status").c_str(), 1, true, "offline")) {
      mqtt.subscribe((base + "/cmd").c_str(), 1);
      mqtt.subscribe("carwash/bays/all/cmd", 1);
      mqtt.publish((base + "/status").c_str(), "online", true);
    } else delay(2000);
  }
}

void onMessage(char* topic, byte* payload, unsigned int len) {
  JsonDocument cmd; deserializeJson(cmd, payload, len);
  const char* action = cmd["action"];
  bool ok = true; const char* err = nullptr;
  if (!strcmp(action, "emergency_stop")) { allRelaysOff(); estop = true; }
  else if (estop && strcmp(action, "reset")) { ok = false; err = "e-stop latched"; }
  else if (!strcmp(action, "start")) { ok = startProgram(cmd["params"]["program"]); }
  // ... stop / pause / resume / reset / set_output / ping
  JsonDocument ack; ack["id"] = cmd["id"]; ack["ok"] = ok; ack["error"] = err;
  char buf[256]; serializeJson(ack, buf);
  mqtt.publish((base + "/ack").c_str(), buf);
}
```

PubSubClient can only *subscribe* at QoS 1. It publishes at QoS 0, which is fine for acks on a local network.

## Safety note

`emergency_stop` over MQTT is a convenience, not a safety system. The firmware must also stop safely on its own when it loses Wi-Fi or the broker, and real e-stops must stay hard-wired.
