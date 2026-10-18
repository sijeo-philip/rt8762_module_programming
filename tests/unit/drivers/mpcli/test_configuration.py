from pathlib import Path

import pytest

from stationapp.drivers.mpcli.configuration import (MpCliProgrammingProfile)


def test_valid_profile() -> None:
    profile = MpCliProgrammingProfile(
        image_packet=Path("stock.bin"),
    )

    assert profile.image_packet == Path("stock.bin")
    assert profile.baud == 1_000_000
    assert profile.reboot_after_programming is True


def test_profile_needs_no_product_id_or_secret_key() -> None:
    # MPCLI's -n/-k pair is optional and not used by the station.
    with pytest.raises(TypeError):
        MpCliProgrammingProfile(
            image_packet=Path("stock.bin"),
            product_id="11223344",
        )


def test_invalid_windows_baud_rejected() -> None:

    with pytest.raises(
        ValueError,
        match="baud",
    ):
        MpCliProgrammingProfile(
            image_packet=Path("stock.bin"),
            baud=115200,
        )
