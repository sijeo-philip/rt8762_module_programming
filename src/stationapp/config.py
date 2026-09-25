""" Application Configuration.

Loaded once at startup from .env, validated by Pydantic, and exposed as a
single frozen settings object. Everything downstream reads settings from here
rather than calling os.environ directly - So configuration is
discoverable in one file andn testable by injecting a different Settings.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """ Validated application settings.
    Field names map to .env keys case-sensitively.
    """

    model_config = SettingsConfigDict(
        env_file = ".env",
        env_file_encodings = "utf-8",
        extra = "ignore",
    )

    # --- Station Identity ---
    station_id: str = Field(..., min_length=1)
    jig_id: str = Field(..., min_length=1)
    jig_positions: Literal[4, 8] = 8

    # --- Local Storage ---
    database_url: str = "sqlite:///./data/station.db"
    log_dir: Path = Path("./logs")

    # ----External Tools ---
    mpcli_path: Path = Path(r"D:\BLE_Projects\StationApp\MPCliTool_v1.0.4.25_Windows\mpcli_v1.0.4.25_Windows\mpcli.exe")
    pricol_app_path: Path | None = None

    # --- Stage 2 ---
    lan_server_url: str | None = None
    lan_server_api_key: str | None = None

    # --- Runtime ---
    log_level: str = "INFO"

    @field_validator("jig_positions", mode="before")
    @classmethod
    def _parse_jig_positions(cls, value):
        """
        Environment variables are read as strings.
        Convert '4' or '8' to integers before literal validation.
        """
        if isinstance(value, str):
            value = value.strip()

        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError("jig_positions must be either 4 or 8")

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
           raise ValueError(f"log_level much be one of {sorted(allowed)}")
        return upper

    @property
    def is_stage_two(self) -> bool:
        """ True once the LAN Server is configured.

        This poperty is read by services to decide whether to attempt server call
        at all. The stage 1 it is always False, so the offline path is normal path
        which is the correct way round: the RFQ requires the staqtion to keep working
        without the server, so offline much be the well-tested default.
        """
        return bool(self.lan_server_url)

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """ Return the process-wide Settings singleton.
    Cache so .env is read once. Tests call get_settings.cache_clear() and
    inject their own environment
    """

    return Settings()
