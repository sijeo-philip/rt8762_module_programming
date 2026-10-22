"""Validated MPCLI programming configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Chip erase before every firmware flash (MPCLI options):
ERASE_FULL = "full"                 # -E  whole flash
ERASE_KEEP_FIRST_4K = "keep_first_4k"   # -C  all but 0x800000-0x801000
ERASE_NONE = "none"                 # no erase
ERASE_MODES = (ERASE_FULL, ERASE_KEEP_FIRST_4K, ERASE_NONE)


@dataclass(frozen=True, slots=True)
class MpCliProgrammingProfile:
    """Configuration required for one MPCLI programming operation.

    image_packet:
        Packed firmware image (-P).

    baud:
        Windows MPCLI documentation recommends 1 Mbps.

    chip_erase:
        Erase the chip (separate MPCLI command, -E by default) before the
        firmware is flashed. See ERASE_MODES.

    MPCLI's optional -n (product ID) and -k (secret key) are not used:
    the validated flash, MAC-write (-x) and read-back commands work
    without them.
    """

    image_packet: Path
    baud: int = 1_000_000
    reboot_after_programming: bool = True
    chip_erase: str = ERASE_FULL

    def __post_init__(self) -> None:
        if self.chip_erase not in ERASE_MODES:
            raise ValueError(f"chip_erase must be one of {', '.join(ERASE_MODES)}")
        if self.baud not in {
            1_000_000,
            2_000_000,
            3_000_000,
        }:
            raise ValueError(
                "Windows MPCLI baud must be "
                "1000000, 2000000 or 3000000"
            )
