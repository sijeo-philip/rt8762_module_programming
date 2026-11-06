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
    # Validated flash -> settle -> write-MAC sequence (see
    # scripts/program_one_real_dut.py) instead of the combined command.
    mpcli_two_step: bool = True
    # Binding slots by reference modules: a module with MAC <prefix>:NN
    # placed in slot NN identifies that slot's COM port.
    slot_reference_mac_prefix: str = "22:22:22:22:22"
    # Chip erase before every firmware flash (Stock and Pricol):
    # full (-E, whole flash) | keep_first_4k (-C) | none
    mpcli_chip_erase: str = "full"
    mpcli_programming_timeout_seconds: float = Field(90.0, gt=0)

    # --- Pricol functional test (PRICOLAPP / PRICOLDFU over the DUT UART) ---
    pricol_uart_baudrate: int = Field(115200, gt=0)
    pricol_response_timeout_seconds: float = Field(3.0, gt=0)
    # After pressing RUN APP / RUN DFU TEST the operator resets the modules;
    # each module's start-up line is awaited this long, then the command
    # is sent.
    pricol_reset_wait_seconds: float = Field(30.0, gt=0, le=600)
    pricol_start_message: str = "### Uart demo interrupt read uart data ###"
    # DTR / RTS levels on the slot's COM port during the functional test:
    # off (the jig requires it). Blank = driver default (pyserial asserts
    # both), only for diagnosis with scripts/uart_monitor.py.
    pricol_uart_dtr: bool | None = False
    pricol_uart_rts: bool | None = False

    # --- Firmware (defaults offered in batch setup) ---
    stock_firmware_path: Path | None = None
    pricol_firmware_path: Path | None = None

    # --- Authorized MAC cache ---
    # Low water: below this many addresses left in the current box block
    # the station fetches the reserve block.
    mac_cache_threshold: int = Field(64, ge=8)

    # --- Temporary Stock MACs (Stock + Pricol mode) ---
    # Slot N is programmed with TEMP_STOCK_MAC_BASE + N - 1 for the Stock
    # firmware and RF test, on every tray; unset, with the built-in
    # TEMPORARY_STOCK_MACS. The box never sees these addresses: the Pricol
    # MAC from a box block replaces them.
    temp_stock_mac_base: str | None = None

    # --- Production ---
    # Accepted module QR content. Tighten once the label format is fixed.
    qr_pattern: str = r"^[!-~]{4,128}$"
    # USB QR scanner (keyboard wedge), as in the legacy tools: keep only
    # hex characters and the last QR_CODE_LENGTH of them. 0 = any length
    # (the scan then ends with the scanner's Enter suffix).
    qr_code_length: int = Field(8, ge=0, le=128)
    qr_hex_only: bool = True
    # A repeat read of the same code within this many milliseconds is
    # discarded (scanner double-read / held trigger). 0 disables.
    qr_debounce_ms: int = Field(2000, ge=0, le=60_000)
    # Also read USB scanners set to HID POS mode (Windows Point of
    # Service), which do not type keystrokes. Keyboard mode always works.
    qr_scanner_hid_pos: bool = True

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

    # --- UnisemBLE box (LAN server), docs/box_integration.md ---
    # BOX_URL is the "Workstation app (SDK base_url)" line on the box
    # installer's finish page; the station key is issued on the box
    # console (Stations page).
    box_url: str | None = None
    box_station_key: str | None = None
    # The master ranges (box console -> Pools) and the block size are
    # set by a Supervisor in Production settings, not here.
    # Unreported modules are re-sent this often while the box is away.
    box_report_retry_seconds: float = Field(60.0, gt=0)

    # --- Login ---
    # server: phone + PIN verified by the box (production).
    # test:   an in-memory demo box with demo users - TESTING ONLY.
    auth_mode: Literal["server", "test"] = "server"

    # --- Runtime ---
    log_level: str = "INFO"

    @field_validator("slot_reference_mac_prefix")
    @classmethod
    def _check_reference_prefix(cls, value: str) -> str:
        from stationapp.services.slot_autobind import normalise_prefix

        return normalise_prefix(value)

    @field_validator("mpcli_chip_erase")
    @classmethod
    def _check_chip_erase(cls, value: str) -> str:
        from stationapp.drivers.mpcli.configuration import ERASE_MODES

        value = value.strip().lower()
        if value not in ERASE_MODES:
            raise ValueError(f"MPCLI_CHIP_ERASE must be one of {', '.join(ERASE_MODES)}")
        return value

    @field_validator("pricol_uart_dtr", "pricol_uart_rts", mode="before")
    @classmethod
    def _blank_is_default(cls, value):
        """A blank DTR / RTS entry means "driver default" (on), not an error."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

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

    @field_validator("temp_stock_mac_base", mode="before")
    @classmethod
    def _check_temp_stock_mac_base(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        from stationapp.domain.mac import MacAddress, temporary_stock_mac

        base = MacAddress.parse(value)
        temporary_stock_mac(base, 8)  # every slot of an 8-up jig must fit
        return base.colon

    @property
    def is_stage_two(self) -> bool:
        """True once the box URL and station key are configured.

        The station keeps producing while the box is unreachable, for as
        long as its blocks last; reports queue until the box answers.
        """
        return bool(self.box_url and self.box_station_key)

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """ Return the process-wide Settings singleton.
    Cache so .env is read once. Tests call get_settings.cache_clear() and
    inject their own environment
    """

    return Settings()
