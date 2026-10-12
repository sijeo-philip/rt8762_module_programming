"""Bring the station database schema up to date at startup.

A station must never run against an outdated schema, so startup applies
any pending Alembic migrations before the UI opens. The migration
scripts ship next to the package (development) or inside the PyInstaller
bundle (installed station).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)


def migrations_directory() -> Path:
    """Locate the alembic/ script directory in dev and frozen builds."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    candidates = []
    if bundle_root is not None:
        candidates.append(Path(bundle_root) / "alembic")
    # src/stationapp/data/schema.py -> repository root
    candidates.append(Path(__file__).resolve().parents[3] / "alembic")

    for candidate in candidates:
        if (candidate / "env.py").is_file():
            return candidate

    raise FileNotFoundError(
        "Alembic migration scripts not found; looked in "
        + ", ".join(str(path) for path in candidates)
    )


def upgrade_to_head(database_url: str) -> None:
    """Apply all pending migrations to database_url."""
    script_location = migrations_directory()

    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", database_url)
    # alembic/env.py reads this override before falling back to settings.
    config.attributes["database_url"] = database_url

    logger.info("Applying database migrations | scripts=%s", script_location)
    command.upgrade(config, "head")
