"""Validated MPCLI programming configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MpCliProgrammingProfile:
    """Configuration required for one MPCLI programming operation.

    image_packet:
        Packed firmware image (-P).

    baud:
        Windows MPCLI documentation recommends 1 Mbps.

    MPCLI's optional -n (product ID) and -k (secret key) are not used:
    the validated flash, MAC-write (-x) and read-back commands work
    without them.
    """

    image_packet: Path
    baud: int = 1_000_000
    reboot_after_programming: bool = True

    def __post_init__(self) -> None:
        if self.baud not in {
            1_000_000,
            2_000_000,
            3_000_000,
        }:
            raise ValueError(
                "Windows MPCLI baud must be "
                "1000000, 2000000 or 3000000"
            )
