"""Request/response models and the command payload sent to the ESP32."""
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

# Bay ids end up inside MQTT topics, so only allow characters that can't
# break the topic tree (no '/', '+', '#').
BAY_ID_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Action(str, Enum):
    START = "start"                    # params: {"program": "basic" | "deluxe" | ...}
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"
    EMERGENCY_STOP = "emergency_stop"
    RESET = "reset"                    # clear a fault / e-stop latch
    SET_OUTPUT = "set_output"          # params: {"output": "pump", "state": true}
    PING = "ping"


class CommandRequest(BaseModel):
    action: Action
    params: dict[str, Any] = Field(default_factory=dict)
    wait_for_ack: bool = Field(
        default=True, description="Block until the ESP32 acks (or the timeout passes)."
    )
    timeout_s: float | None = Field(default=None, gt=0, le=30)

    @model_validator(mode="after")
    def _check_params(self) -> "CommandRequest":
        if self.action is Action.START and "program" not in self.params:
            raise ValueError("start requires params.program")
        if self.action is Action.SET_OUTPUT:
            if "output" not in self.params or not isinstance(self.params.get("state"), bool):
                raise ValueError("set_output requires params.output and boolean params.state")
        return self


class CommandStatus(str, Enum):
    SENT = "sent"          # published, waiting for ack
    ACKED = "acked"        # ESP32 accepted and ran it
    REJECTED = "rejected"  # ESP32 replied ok=false
    TIMEOUT = "timeout"    # no ack in time


class CommandMessage(BaseModel):
    """Exact JSON published to carwash/bays/<bay>/cmd."""
    id: str = Field(default_factory=lambda: uuid4().hex)
    action: Action
    params: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)


class CommandRecord(BaseModel):
    id: str
    bay_id: str
    action: Action
    params: dict[str, Any]
    status: CommandStatus
    sent_at: datetime
    acked_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


class BayInfo(BaseModel):
    bay_id: str
    online: bool
    last_seen: datetime | None = None
    state: dict[str, Any] | None = None  # last telemetry/state message, if any


class HealthResponse(BaseModel):
    status: str
    mqtt_connected: bool
    broker: str
