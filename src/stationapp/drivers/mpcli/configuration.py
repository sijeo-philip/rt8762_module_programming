"""Validated MPCLI programming configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_HEX_8 = re.compile(
    r"^[0-9A-Fa-f]{8}$"
)

_HEX_64 = re.compile(
    r"^[0-9A-Fa-f]{64}$"
)


@dataclass(frozen=True, slots=True)
class MpCliProgrammingProfile:
    """Configuration required for one MPCLI programming operation.

    product_id:
        Four bytes represented by exactly eight hexadecimal characters.

    secret_key:
        Thirty-two bytes represented by exactly sixty-four
        hexadecimal characters.

    baud:
        Windows MPCLI documentation recommends 1 Mbps.
    """

    image_packet: Path
    product_id: str
    secret_key: str
    baud: int = 1_000_000
    reboot_after_programming: bool = True

    def __post_init__(self) -> None:
        product_id = (
            self.product_id
            .strip()
            .replace(":", "")
            .replace("-", "")
            .replace(" ", "")
        )
        secret_key = (
            self.secret_key
            .strip()
            .replace(":", "")
            .replace("-", "")
            .replace(" ", "")
        )

        if not _HEX_8.fullmatch(product_id):
            raise ValueError(
                "product_id must contain exactly "
                "4 bytes / 8 hexadecimal characters"
            )

        if not _HEX_64.fullmatch(secret_key):
            raise ValueError(
                "secret_key must contain exactly "
                "32 bytes / 64 hexadecimal characters"
            )

        if self.baud not in {
            1_000_000,
            2_000_000,
            3_000_000,
        }:
            raise ValueError(
                "Windows MPCLI baud must be "
                "1000000, 2000000 or 3000000"
            )

        object.__setattr__(self, "product_id", product_id.upper())
        object.__setattr__(self, "secret_key", secret_key.upper())


