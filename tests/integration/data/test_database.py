

"""Integration tests for station SQLite engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from stationapp.data.database import (DEFAULT_BUSY_TIMEOUT_MS, create_session_factory, create_station_engine)

@pytest.mark.integration
def test_sqlite_database_file_is_created(tmp_path: Path) -> None:
    database_file = tmp_path / "station.db"
    
    engine = create_station_engine(f"sqlite:///{database_file.as_posix()}")
    
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT 1"))
            
        assert database_file.exists()
        
    finally:
        engine.dispose()
        
        
@pytest.mark.integration
def test_sqlite_foreign_keys_are_enabled(tmp_path: Path) -> None:
    database_file = tmp_path / "station.db"
    
    engine = create_station_engine(f"sqlite:///{database_file.as_posix()}")
    
    try:
        with engine.connect() as connection:
            value = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
            
        assert value == 1
        
    finally:
        engine.dispose()
        
@pytest.mark.integration
def test_sqlite_usee_wal_mode(tmp_path: Path) -> None:
    database_file = tmp_path / "station.db"

    engine = create_station_engine(
        f"sqlite:///{database_file.as_posix()}"
    )

    try:
        with engine.connect() as connection:
            value = connection.execute(
                text("PRAGMA journal_mode")
            ).scalar_one()

        assert str(value).lower() == "wal"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_sqlite_uses_full_synchronous_mode(
    tmp_path: Path,
) -> None:
    database_file = tmp_path / "station.db"

    engine = create_station_engine(
        f"sqlite:///{database_file.as_posix()}"
    )

    try:
        with engine.connect() as connection:
            value = connection.execute(
                text("PRAGMA synchronous")
            ).scalar_one()

        # SQLite numeric value:
        # 0 = OFF
        # 1 = NORMAL
        # 2 = FULL
        # 3 = EXTRA
        assert value == 2
    finally:
        engine.dispose()


@pytest.mark.integration
def test_sqlite_busy_timeout_is_configured(
    tmp_path: Path,
) -> None:
    database_file = tmp_path / "station.db"

    engine = create_station_engine(
        f"sqlite:///{database_file.as_posix()}"
    )

    try:
        with engine.connect() as connection:
            value = connection.execute(
                text("PRAGMA busy_timeout")
            ).scalar_one()

        assert value == DEFAULT_BUSY_TIMEOUT_MS
    finally:
        engine.dispose()


@pytest.mark.integration
def test_session_factory_creates_working_session(
    tmp_path: Path,
) -> None:
    database_file = tmp_path / "station.db"

    engine = create_station_engine(
        f"sqlite:///{database_file.as_posix()}"
    )

    session_factory = create_session_factory(engine)

    try:
        with session_factory() as session:
            value = session.execute(
                text("SELECT 123")
            ).scalar_one()

        assert value == 123
    finally:
        engine.dispose()