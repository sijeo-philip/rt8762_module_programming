"""One conversation with the Golden Rig at a time, station-wide.

The rig handles one RF request at a time. Two requests (or an identity
probe arriving during an RF exchange) would clash and fail modules for no
reason. Every connection the station makes to the rig therefore holds
GOLDEN_RIG_LOCK for its whole duration:

* RF tests (the tray's test and each operator RETRY of one slot) wait for
  the lock, so they run strictly one after another;
* supervisor checks and the READY reachability probe never wait: while an
  RF test holds the lock they report the rig as busy instead of connecting.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

GOLDEN_RIG_LOCK = threading.Lock()

# Longest an RF test waits for a supervisor check (bounded by its own
# short network timeouts) to release the rig.
RF_WAIT_SECONDS = 30.0


class GoldenRigBusy(Exception):
    """The rig is in use by another exchange from this station."""


@contextmanager
def golden_rig_exchange(purpose: str, *, wait: bool = True, timeout: float = RF_WAIT_SECONDS) -> Iterator[None]:
    """Hold the rig for one exchange. wait=False: raise GoldenRigBusy at
    once if it is in use; otherwise wait up to timeout seconds."""
    acquired = GOLDEN_RIG_LOCK.acquire(timeout=timeout) if wait else GOLDEN_RIG_LOCK.acquire(blocking=False)
    if not acquired:
        raise GoldenRigBusy(f"The Golden Rig is busy with another request ({purpose} not started)")
    logger.debug("Golden Rig held for %s", purpose)
    try:
        yield
    finally:
        GOLDEN_RIG_LOCK.release()
