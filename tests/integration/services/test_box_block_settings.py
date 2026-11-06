"""Box master ranges and block size come from the Supervisor's production
settings, not from .env: request_block(size, master_range) uses what was
saved, a range change closes the old range's blocks, and saving is
validated."""

from __future__ import annotations

from pathlib import Path

import pytest
import unisemble_client.client as sdk_client

from stationapp.data.base import Base
from stationapp.data.database import create_session_factory, create_station_engine
from stationapp.domain.batch import ProgrammingMode
from stationapp.domain.mac import MacPurpose
from stationapp.drivers.box.demo import DemoBox
from stationapp.services.box_blocks import BoxBlockService
from stationapp.services.production_settings import (
    DEFAULT_BLOCK_SIZE,
    ProductionSettings,
    ProductionSettingsService,
)

PRICOL = MacPurpose.PRICOL_PRODUCTION
STOCK = MacPurpose.STOCK_RF_TEST


@pytest.fixture(autouse=True)
def _no_retry_waits(monkeypatch):
    monkeypatch.setattr(sdk_client, "_sleep", lambda _seconds: None)


@pytest.fixture
def factory(tmp_path: Path):
    engine = create_station_engine(f"sqlite:///{(tmp_path / 'station.db').as_posix()}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def settings(**overrides) -> ProductionSettings:
    values = dict(
        mode=ProgrammingMode.STOCK_AND_PRICOL,
        stock_firmware=Path("stock.bin"),
        pricol_firmware=Path("pricol.bin"),
        enabled_slots=(1, 2),
        stock_master_range="STOCK-A",
        pricol_master_range="PRICOL-A",
        block_size=200,
    )
    values.update(overrides)
    return ProductionSettings(**values)


def wired(factory, box: DemoBox):
    service = ProductionSettingsService(session_factory=factory, station_id="STN", jig_positions=8, defaults=settings())
    blocks = BoxBlockService(session_factory=factory, gateway=box.gateway(), station_id="STN", jig_id="JIG",
                             master_ranges={}, block_size=DEFAULT_BLOCK_SIZE, low_water=8)
    blocks.use_settings(master_ranges=lambda purpose: service.current().master_range(purpose),
                        block_size=lambda: service.current().block_size)
    service.add_listener(blocks.apply_settings)
    return service, blocks


def test_block_requested_with_supervisor_range_and_size(factory):
    box = DemoBox(master_ranges=("STOCK-A", "PRICOL-A"))
    service, blocks = wired(factory, box)
    service.update(settings(block_size=300), reason="setup")

    blocks.ensure(PRICOL)

    (block,) = box.blocks
    assert block["range"] == "PRICOL-A"
    assert block["end"] - block["start"] + 1 == 300
    assert blocks.state(PRICOL).current.master_range == "PRICOL-A"


def test_block_size_change_applies_to_next_block_only(factory):
    box = DemoBox(master_ranges=("STOCK-A", "PRICOL-A"))
    service, blocks = wired(factory, box)
    service.update(settings(block_size=100), reason="setup")
    blocks.ensure(PRICOL)
    service.update(settings(block_size=150), reason="bigger blocks")

    # Current block (100) is still held; with low water above it, a reserve is fetched.
    blocks._low_water = 1000
    blocks.ensure(PRICOL)

    sizes = [b["end"] - b["start"] + 1 for b in box.blocks]
    assert sizes == [100, 150]
    state = blocks.state(PRICOL)
    assert state.current.size == 100 and state.reserve.size == 150


def test_range_change_closes_old_blocks_and_draws_from_new_range(factory):
    box = DemoBox(master_ranges=("STOCK-A", "PRICOL-A", "PRICOL-B"))
    service, blocks = wired(factory, box)
    service.update(settings(), reason="setup")
    blocks._low_water = 1000
    blocks.ensure(PRICOL)                          # current from PRICOL-A
    blocks.ensure(PRICOL)                          # reserve from PRICOL-A
    assert blocks.state(PRICOL).reserve is not None

    service.update(settings(pricol_master_range="PRICOL-B"), reason="new pool")
    assert blocks.state(PRICOL).current is None    # old range closed at once, offline too

    blocks._low_water = 8
    status = blocks.ensure(PRICOL)

    state = blocks.state(PRICOL)
    assert state.current.master_range == "PRICOL-B", status.message
    assert box.blocks[-1]["range"] == "PRICOL-B"
    # The box's old pair was promoted (closed) so the new block could be issued.
    assert [b["state"] for b in box.blocks[:2]] == ["closed", "open"]


def test_no_range_set_reports_where_to_set_it(factory):
    box = DemoBox(master_ranges=("STOCK-A", "PRICOL-A"))
    service, blocks = wired(factory, box)
    service._defaults = settings(pricol_master_range=None)     # nothing saved yet

    status = blocks.ensure(PRICOL)

    assert box.blocks == []
    assert "Production settings" in status.message


def test_unknown_range_is_reported(factory):
    box = DemoBox(master_ranges=("STOCK-A", "PRICOL-A"))
    service, blocks = wired(factory, box)
    service.update(settings(pricol_master_range="TYPO"), reason="setup")

    status = blocks.ensure(PRICOL)

    assert "no master range of that name" in status.message


@pytest.mark.parametrize("overrides, message", [
    (dict(mode=ProgrammingMode.STOCK_AND_PRICOL, pricol_master_range=None), r"Stock \+ Pricol"),
    (dict(mode=ProgrammingMode.STOCK_ONLY, stock_master_range=None, pricol_firmware=None), "Stock Only"),
    (dict(block_size=99), "Block size"),
    (dict(block_size=20001), "Block size"),
    (dict(stock_master_range=" X"), "no leading or trailing"),
])
def test_validation(factory, overrides, message):
    service = ProductionSettingsService(session_factory=factory, station_id="STN", jig_positions=8, defaults=settings())
    with pytest.raises(ValueError, match=message):
        service.update(settings(**overrides), reason="x")


def test_only_the_range_for_the_mode_is_required(factory):
    service = ProductionSettingsService(session_factory=factory, station_id="STN", jig_positions=8, defaults=settings())
    service.update(settings(stock_master_range=None), reason="Stock + Pricol needs the Pricol range only")
    service.update(settings(mode=ProgrammingMode.STOCK_ONLY, pricol_master_range=None, pricol_firmware=None), reason="Stock Only")


def test_demo_box_does_not_require_ranges(factory):
    service = ProductionSettingsService(session_factory=factory, station_id="STN", jig_positions=8,
                                        defaults=settings(), require_master_ranges=False)
    service.update(settings(stock_master_range=None, pricol_master_range=None), reason="demo")


def test_settings_saved_before_this_change_still_load():
    old = {"mode": "STOCK_AND_PRICOL", "stock_firmware": "s.bin", "pricol_firmware": "p.bin", "enabled_slots": [1], "lot": None}
    loaded = ProductionSettings.from_dict(old)
    assert loaded.stock_master_range is None and loaded.pricol_master_range is None
    assert loaded.block_size == DEFAULT_BLOCK_SIZE
    assert ProductionSettings.from_dict(settings().to_dict()) == settings()
