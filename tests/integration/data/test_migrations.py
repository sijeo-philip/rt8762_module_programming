"""Integration tests for Alembic schema migration."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


EXPECTED_TABLES = {
    "allocations",
    "allocated_macs",
    "batches",
    "batch_slots",
    "programming_events",
    "verification_events",
    "manufacturing_events",
    "audit_records",
    "outbox",
}


@pytest.mark.integration
def test_upgrade_head_creates_station_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_file = tmp_path / "migration_test.db"

    database_url = (
        f"sqlite:///{database_file.as_posix()}"
    )

    monkeypatch.setenv(
        "STATIONAPP_MIGRATION_DATABASE_URL",
        database_url,
    )

    config = Config("alembic.ini")

    command.upgrade(
        config,
        "head",
    )

    engine = create_engine(database_url)

    try:
        inspector = inspect(engine)

        tables = set(inspector.get_table_names())

        assert EXPECTED_TABLES.issubset(tables)
        assert "alembic_version" in tables
    finally:
        engine.dispose()

@pytest.mark.integration
def test_migration_can_downgrade_and_reupgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_file = tmp_path / "roundtrip.db"

    database_url = (
        f"sqlite:///{database_file.as_posix()}"
    )

    monkeypatch.setenv(
        "STATIONAPP_MIGRATION_DATABASE_URL",
        database_url,
    )

    config = Config("alembic.ini")

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    engine = create_engine(database_url)

    try:
        inspector = inspect(engine)

        tables = set(inspector.get_table_names())

        assert EXPECTED_TABLES.issubset(tables)
    finally:
        engine.dispose()


