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

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """ Validated application settings.
    Field names map to .env keys case-sensitively.
    """

    model_config = SettingsConfigDict(
        env_file = ".env",
        env_file_encoding = "utf-8",
        extra = "ignore",
    )

    # --- Station Identity ---
    station_id: str = Field(..., min_length=1)
    jig_id: str = Field(..., min_length=1)
    jig_positions: Literal[4, 8] = 8

    # --- Local Storage ---
    database_url: str = "sqlite:///./data/station.db"
    log_dir: Path = Path("./logs")
    serial_binding_file: Path = Path("./data/serial_bindings.json")
    golden_rig_binding_file: Path = Path("./data/golden_rig_binding.json")
    golden_rig_ca_certificate: Path = Path("./config/certificates/golden_rig_ca.pem")
    

    # ----External Tools ---
    mpcli_path: Path = Path(r"D:\BLE_Projects\StationApp\MPCliTool_v1.0.4.25_Windows\mpcli_v1.0.4.25_Windows\mpcli.exe")
    pricol_app_path: Path | None = None
    mpcli_product_id: str | None = None
    mpcli_secret_key: SecretStr | None = None
    # Validated flash -> settle -> write-MAC sequence (see
    # scripts/program_one_real_dut.py) instead of the combined command.
    mpcli_two_step: bool = True
    mpcli_programming_timeout_seconds: float = Field(90.0, gt=0)

    # --- Pricol functional test (PRICOLAPP / PRICOLDFU over the DUT UART) ---
    pricol_uart_baudrate: int = Field(115200, gt=0)
    pricol_response_timeout_seconds: float = Field(3.0, gt=0)

    # --- Firmware (defaults offered in batch setup) ---
    stock_firmware_path: Path | None = None
    pricol_firmware_path: Path | None = None

    # --- Authorized MAC cache ---
    # Below this many AVAILABLE MACs per purpose, the station requests
    # another block from the LAN server.
    mac_cache_threshold: int = Field(64, ge=8)
    mac_request_size: int = Field(200, ge=8)

    # --- Production ---
    # Accepted module QR content. Tighten once the label format is fixed.
    qr_pattern: str = r"^[!-~]{4,128}$"

    # --- Access control ---
    # Supervisor elevation lapses after this much inactivity.
    supervisor_timeout_minutes: int = Field(15, ge=1)
    auth_max_failures: int = Field(5, ge=1)
    auth_lockout_minutes: int = Field(5, ge=1)

    # --- Golden Rig binding ---
    # Approved rig IDs (certificate common names). Empty = any rig whose
    # certificate is signed by GOLDEN_RIG_CA_CERTIFICATE.
    golden_rig_inventory: str = ""
    # Rigs offered by "Discover": host:port,host:port
    golden_rig_candidates: str = ""

    # --- Simulation (development / training only) ---
    # Replaces MPCLI, the Golden Rig, functional tests, serial ports and
    # MAC allocation with simulators. Never enable on a production station.
    simulation_mode: bool = False
    simulation_failures: str = ""
    simulation_delay_seconds: float = Field(1.0, ge=0)

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
