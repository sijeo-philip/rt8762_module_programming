from pathlib import Path

import pytest

from stationapp.drivers.mpcli.configuration import (MpCliProgrammingProfile)

VALID_KEY = (
    "00112233445566778899AABBCCDDEEFF"
    "00112233445566778899AABBCCDDEEFF"
)


def test_valid_profile() -> None:
    profile = MpCliProgrammingProfile(
        image_packet=Path("stock.bin"),
        product_id="11223344",
        secret_key=VALID_KEY,
    )

    assert profile.product_id == "11223344"
    assert profile.secret_key == VALID_KEY
    assert profile.baud == 1_000_000


def test_product_id_can_have_separators() -> None:

    profile = MpCliProgrammingProfile(
        image_packet=Path("stock.bin"),
        product_id="11:22:33:44",
        secret_key=VALID_KEY,
    )

    assert profile.product_id == "11223344"


def test_invalid_product_id_rejected() -> None:

    with pytest.raises(
        ValueError,
        match="product_id",
    ):
        MpCliProgrammingProfile(
            image_packet=Path("stock.bin"),
            product_id="1234",
            secret_key=VALID_KEY,
        )


def test_invalid_secret_key_rejected() -> None:

    with pytest.raises(
        ValueError,
        match="secret_key",
    ):
        MpCliProgrammingProfile(
            image_packet=Path("stock.bin"),
            product_id="11223344",
            secret_key="1234",
        )


def test_invalid_windows_baud_rejected() -> None:

    with pytest.raises(
        ValueError,
        match="baud",
    ):
        MpCliProgrammingProfile(
            image_packet=Path("stock.bin"),
            product_id="11223344",
            secret_key=VALID_KEY,
            baud=115200,
        )


