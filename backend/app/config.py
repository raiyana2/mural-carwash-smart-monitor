"""Runtime settings, read from environment variables or a .env file."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CARWASH_", extra="ignore")

    # MQTT broker
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_client_id: str = "carwash-backend"
    mqtt_keepalive: int = 30

    # Root of every topic, e.g. carwash/bays/bay1/cmd
    topic_prefix: str = "carwash"

    # How long to wait for an ESP32 ack when wait_for_ack=true
    default_ack_timeout_s: float = 5.0
    # How many finished commands to remember for GET /commands/{id}
    command_history_size: int = 500

    # If set, every request must send header  X-API-Key: <value>
    api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
