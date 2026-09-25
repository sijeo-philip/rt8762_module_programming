""" Application bootstrap.

Wires the pieces that much exis before anything else runs, in the right order:
1. settings     -- Validated configuration
2. Logging     -- Configured uisng those settings
3. runtime context -- an immutable object handed to the UI and services

Doing this in one places (rather than scattering get_settings() and setup_logging() calls through
the UI) means there is exactly one startup path, which is the one that gets tested and later
gets the health checks.
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass
from datetime import datetime, timezone

from stationapp.config import Settings, get_settings
from stationapp.logging_setup import setup_logging

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class AppContext:
    """ Immutable bundle of everything the app needs at runtime.
    Passed explicitly to the UI and to services, rather than the reaching for
    globals. This is direct antidote to the pattern in Clent_8jig_qr.txt,
    where 'current_user', 'client_socket', and 'mac_enteries' are module-level
    globals mutated from everywhere -- which is why that file has no tests
    and why nothing survives a restart.
    """

    settings: Settings
    started_at: datetime

    @property
    def app_version(self) -> str:
        from stationapp import __version__
        return __version__

    @property
    def station_label(self) -> str:
        return f"{self.settings.station_id} / (self.settings.jig_id)"

def bootstrap() -> AppContext:
    """ Initialize the application runtime and return its context.

    Raises:
        pydantic.ValidationError: if configuration is missing or invalid.
        Delibrately NOT caught here -- a misconfigured station must fail loudly
        at startup, not run with defaults and corrupt the traceability record.
    """
    settings = get_settings()
    print(settings.jig_positions)
    print(type(settings.jig_positions))
    app_logger = setup_logging(settings.log_dir, settings.log_level)
    app_logger.info("-"*60)
    app_logger.info(" Station App is %s starting | station=%s jig=%s position=%d",
    _app_version(), settings.station_id, settings.jig_id, settings.jig_positions)
    app_logger.info("Host: %s | Python %s", platform.node(), platform.python_version())
    app_logger.info("Stage 2 (LAN SERVER) %s", "configured" if settings.is_stage_two else
    "not configure -- offline mode")
    context = AppContext( settings=settings, started_at=datetime.now(timezone.utc))
    logger.debug("Bootstrap Complete")
    return context

def _app_version() -> str:
    from stationapp import __version__
    return __version__
