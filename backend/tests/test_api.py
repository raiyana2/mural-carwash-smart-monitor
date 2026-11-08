"""End-to-end tests: real broker + fake ESP32 + the API.

Needs a broker on localhost:1883 (docker compose up -d, or a local mosquitto).
Skipped automatically if none is reachable.
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def _broker_up() -> bool:
    try:
        socket.create_connection(("localhost", 1883), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _broker_up(), reason="no MQTT broker on localhost:1883")


@pytest.fixture(scope="module")
def bay():
    p = subprocess.Popen([sys.executable, str(ROOT / "tools/fake_esp32.py"), "--bay", "testbay"])
    time.sleep(1)
    yield "testbay"
    p.terminate()
    p.wait()


@pytest.fixture(scope="module")
def client(bay):
    from app.main import app
    with TestClient(app) as c:
        for _ in range(50):
            if c.get("/health").json()["mqtt_connected"] and c.get(f"/bays/{bay}").status_code == 200:
                break
            time.sleep(0.1)
        yield c


def test_health(client):
    assert client.get("/health").json()["mqtt_connected"] is True


def test_bay_reports_online(client, bay):
    b = client.get(f"/bays/{bay}").json()
    assert b["online"] is True


def test_start_then_busy_then_stop(client, bay):
    r = client.post(f"/bays/{bay}/commands", json={"action": "start", "params": {"program": "deluxe"}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "acked"
    assert r.json()["result"]["mode"] == "washing"

    r = client.post(f"/bays/{bay}/commands", json={"action": "start", "params": {"program": "basic"}})
    assert r.status_code == 409  # bay refuses: already washing

    r = client.post(f"/bays/{bay}/commands", json={"action": "stop"})
    assert r.status_code == 200


def test_async_send_and_poll(client, bay):
    r = client.post(f"/bays/{bay}/commands", json={"action": "ping", "wait_for_ack": False})
    assert r.json()["status"] == "sent"
    cid = r.json()["id"]
    for _ in range(30):
        if client.get(f"/commands/{cid}").json()["status"] == "acked":
            break
        time.sleep(0.1)
    assert client.get(f"/commands/{cid}").json()["status"] == "acked"


def test_timeout_for_missing_bay(client):
    r = client.post("/bays/ghost/commands", json={"action": "ping", "timeout_s": 0.5})
    assert r.status_code == 504


def test_estop_broadcast_latches(client, bay):
    assert client.post("/bays/all/emergency-stop").status_code == 200
    time.sleep(0.4)
    r = client.post(f"/bays/{bay}/commands", json={"action": "start", "params": {"program": "basic"}})
    assert r.status_code == 409
    assert client.post(f"/bays/{bay}/commands", json={"action": "reset"}).status_code == 200


def test_validation(client, bay):
    assert client.post(f"/bays/{bay}/commands", json={"action": "start"}).status_code == 422
    assert client.post(f"/bays/{bay}/commands", json={"action": "explode"}).status_code == 422
    assert client.post("/bays/bad+id/commands", json={"action": "ping"}).status_code == 422
