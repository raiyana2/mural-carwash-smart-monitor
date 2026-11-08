"""FastAPI app: REST endpoints that turn HTTP calls into MQTT commands for the ESP32 bays.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
Docs: http://<pi>:8000/docs
"""
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, status
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings, get_settings
from .models import (
    BAY_ID_PATTERN,
    Action,
    BayInfo,
    CommandMessage,
    CommandRecord,
    CommandRequest,
    CommandStatus,
    HealthResponse,
)
from .mqtt_client import BROADCAST_ID, BrokerUnavailable, MqttBridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    bridge = MqttBridge(get_settings())
    bridge.start()
    app.state.mqtt = bridge
    yield
    bridge.stop()


app = FastAPI(title="Car Wash Control API", version="0.1.0", lifespan=lifespan)
app.add_middleware(  # the dashboard may be served from another port
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def get_bridge() -> MqttBridge:
    return app.state.mqtt


def check_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing X-API-Key")


BayId = Annotated[str, Path(pattern=BAY_ID_PATTERN, description="e.g. bay1")]
Bridge = Annotated[MqttBridge, Depends(get_bridge)]
protected = [Depends(check_api_key)]


@app.get("/health", response_model=HealthResponse)
def health(bridge: Bridge, settings: Annotated[Settings, Depends(get_settings)]):
    return HealthResponse(
        status="ok" if bridge.connected else "degraded",
        mqtt_connected=bridge.connected,
        broker=f"{settings.mqtt_host}:{settings.mqtt_port}",
    )


@app.get("/bays", response_model=list[BayInfo], dependencies=protected)
def list_bays(bridge: Bridge):
    """Every bay that has published a status/ack/state message since startup."""
    return bridge.list_bays()


@app.get("/bays/{bay_id}", response_model=BayInfo, dependencies=protected)
def get_bay(bay_id: BayId, bridge: Bridge):
    bay = bridge.get_bay(bay_id)
    if not bay:
        raise HTTPException(404, f"bay '{bay_id}' has not reported in")
    return bay


async def _send(bridge: MqttBridge, bay_id: str, req: CommandRequest) -> CommandRecord:
    msg = CommandMessage(action=req.action, params=req.params)
    try:
        rec = await bridge.send_command(bay_id, msg, req.wait_for_ack, req.timeout_s)
    except BrokerUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    if rec.status is CommandStatus.TIMEOUT:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"message": f"no ack from {bay_id}", "command": rec.model_dump(mode="json")},
        )
    if rec.status is CommandStatus.REJECTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": rec.error or "rejected by bay", "command": rec.model_dump(mode="json")},
        )
    return rec


@app.post("/bays/{bay_id}/commands", response_model=CommandRecord, dependencies=protected)
async def send_command(bay_id: BayId, req: CommandRequest, bridge: Bridge):
    """Send one command to one bay.

    - wait_for_ack=true (default): returns 200 once the ESP32 acks, 504 on timeout, 409 if it refuses.
    - wait_for_ack=false: returns immediately with status "sent"; poll GET /commands/{id}.
    """
    if bay_id == BROADCAST_ID:
        raise HTTPException(400, "use POST /bays/all/emergency-stop for broadcasts")
    return await _send(bridge, bay_id, req)


@app.post("/bays/all/emergency-stop", response_model=CommandRecord, dependencies=protected)
async def emergency_stop_all(bridge: Bridge):
    """Broadcast emergency_stop to every bay. Fire-and-forget: acks come back per bay."""
    return await _send(bridge, BROADCAST_ID, CommandRequest(action=Action.EMERGENCY_STOP, wait_for_ack=False))


@app.get("/commands/{cmd_id}", response_model=CommandRecord, dependencies=protected)
def get_command(cmd_id: str, bridge: Bridge):
    rec = bridge.get_command(cmd_id)
    if not rec:
        raise HTTPException(404, "unknown command id")
    return rec
