"""Request bodies the web API accepts. FastAPI checks them before a route runs."""

from typing import Optional

from pydantic import BaseModel, Field

from carwash.config import BAY_COUNT, MAX_MOTOR_SECONDS


class ConnectRequest(BaseModel):
    port: Optional[str] = None


class SessionRequest(BaseModel):
    seconds: int = Field(ge=1, le=MAX_MOTOR_SECONDS)
    wash_mode: Optional[str] = None


class SettingsRequest(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    enabled: bool
    default_wash_mode: str
    base_price_cents: int = Field(ge=0, le=100000)
    included_seconds: int = Field(ge=0, le=MAX_MOTOR_SECONDS)
    additional_price_per_minute_cents: int = Field(ge=0, le=100000)
    maximum_duration_seconds: int = Field(ge=1, le=MAX_MOTOR_SECONDS)


class EnabledRequest(BaseModel):
    enabled: bool


class PreviewRequest(BaseModel):
    bay_number: int = Field(ge=1, le=BAY_COUNT)
    seconds: int = Field(ge=1, le=MAX_MOTOR_SECONDS)


class ScenarioRequest(BaseModel):
    bay_number: int = Field(ge=2, le=BAY_COUNT)
    scenario: str
