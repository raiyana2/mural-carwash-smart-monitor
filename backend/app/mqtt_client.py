"""MQTT bridge: publishes commands, tracks acks, and tracks which bays are online.

Topic contract (prefix defaults to "carwash"):

    carwash/bays/<bay_id>/cmd     backend -> ESP32   command JSON (QoS 1)
    carwash/bays/all/cmd          backend -> ESP32   broadcast (e.g. emergency_stop)
    carwash/bays/<bay_id>/ack     ESP32 -> backend   {"id": "...", "ok": true, "error": null, "result": {...}}
    carwash/bays/<bay_id>/status  ESP32 -> backend   "online" / "offline"  (retained; "offline" set as LWT)
    carwash/bays/<bay_id>/state   ESP32 -> backend   any JSON telemetry (optional)

paho runs its network loop on its own thread; anything that touches asyncio
objects is handed back to the event loop with call_soon_threadsafe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import OrderedDict
from datetime import datetime

import paho.mqtt.client as mqtt

from .config import Settings
from .models import (
    BayInfo,
    CommandMessage,
    CommandRecord,
    CommandStatus,
    utcnow,
)

log = logging.getLogger(__name__)

BROADCAST_ID = "all"


class BrokerUnavailable(RuntimeError):
    pass


class MqttBridge:
    def __init__(self, settings: Settings):
        self.s = settings
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._connected = threading.Event()

        self._pending: dict[str, asyncio.Future] = {}
        self._records: OrderedDict[str, CommandRecord] = OrderedDict()
        self._bays: dict[str, BayInfo] = {}

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id,
            protocol=mqtt.MQTTv311,
        )
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ---- topics -------------------------------------------------------
    def topic(self, bay_id: str, leaf: str) -> str:
        return f"{self.s.topic_prefix}/bays/{bay_id}/{leaf}"

    # ---- lifecycle ----------------------------------------------------
    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        # connect_async + loop_start: never blocks startup, and paho keeps
        # retrying in the background if the broker is down or drops.
        self.client.connect_async(self.s.mqtt_host, self.s.mqtt_port, self.s.mqtt_keepalive)
        self.client.loop_start()
        log.info("MQTT connecting to %s:%s", self.s.mqtt_host, self.s.mqtt_port)

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout: float) -> bool:
        return await asyncio.to_thread(self._connected.wait, timeout)

    # ---- paho callbacks (network thread) ------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        log.info("MQTT connected")
        self._connected.set()
        p = self.s.topic_prefix
        client.subscribe([
            (f"{p}/bays/+/ack", 1),
            (f"{p}/bays/+/status", 1),
            (f"{p}/bays/+/state", 0),
        ])

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected.clear()
        log.warning("MQTT disconnected (%s); will retry", reason_code)

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        parts = msg.topic.split("/")
        if len(parts) != 4 or parts[1] != "bays":
            return
        bay_id, leaf = parts[2], parts[3]
        raw = msg.payload.decode("utf-8", errors="replace").strip()
        now = utcnow()

        with self._lock:
            bay = self._bays.setdefault(bay_id, BayInfo(bay_id=bay_id, online=False))
            if leaf == "status":
                bay.online = raw.lower() == "online"
                bay.last_seen = now
                return
            bay.last_seen = now
            bay.online = True  # it just talked to us

        if leaf == "state":
            try:
                with self._lock:
                    bay.state = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("bad state JSON from %s: %r", bay_id, raw)
        elif leaf == "ack":
            self._handle_ack(bay_id, raw, now)

    def _handle_ack(self, bay_id: str, raw: str, now: datetime) -> None:
        try:
            ack = json.loads(raw)
            cmd_id = str(ack["id"])
        except (json.JSONDecodeError, KeyError, TypeError):
            log.warning("bad ack from %s: %r", bay_id, raw)
            return

        with self._lock:
            rec = self._records.get(cmd_id)
            if rec is None:
                return
            rec.acked_at = now
            rec.status = CommandStatus.ACKED if ack.get("ok", True) else CommandStatus.REJECTED
            rec.error = ack.get("error")
            rec.result = ack.get("result")
            fut = self._pending.pop(cmd_id, None)

        if fut and self._loop:
            self._loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(None))

    # ---- public API (event loop) --------------------------------------
    async def send_command(
        self, bay_id: str, msg: CommandMessage, wait_for_ack: bool, timeout_s: float | None
    ) -> CommandRecord:
        if not self.connected:
            raise BrokerUnavailable("not connected to MQTT broker")

        rec = CommandRecord(
            id=msg.id, bay_id=bay_id, action=msg.action, params=msg.params,
            status=CommandStatus.SENT, sent_at=msg.ts,
        )
        fut = asyncio.get_running_loop().create_future() if wait_for_ack else None
        with self._lock:
            self._remember(rec)
            if fut:
                self._pending[msg.id] = fut

        info = self.client.publish(self.topic(bay_id, "cmd"), msg.model_dump_json(), qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            with self._lock:
                self._pending.pop(msg.id, None)
                self._records.pop(msg.id, None)
            raise BrokerUnavailable(f"publish failed: {mqtt.error_string(info.rc)}")

        if fut:
            timeout = timeout_s or self.s.default_ack_timeout_s
            try:
                await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                with self._lock:
                    self._pending.pop(msg.id, None)
                    if rec.status is CommandStatus.SENT:
                        rec.status = CommandStatus.TIMEOUT
        return rec.model_copy()

    def get_command(self, cmd_id: str) -> CommandRecord | None:
        with self._lock:
            rec = self._records.get(cmd_id)
            return rec.model_copy() if rec else None

    def list_bays(self) -> list[BayInfo]:
        with self._lock:
            return [b.model_copy() for b in sorted(self._bays.values(), key=lambda b: b.bay_id)]

    def get_bay(self, bay_id: str) -> BayInfo | None:
        with self._lock:
            b = self._bays.get(bay_id)
            return b.model_copy() if b else None

    def _remember(self, rec: CommandRecord) -> None:
        self._records[rec.id] = rec
        while len(self._records) > self.s.command_history_size:
            self._records.popitem(last=False)
