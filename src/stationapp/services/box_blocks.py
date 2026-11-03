"""Blocks of MAC addresses from the box (workflow steps 5-6).

The box owns the site's addresses and gives this station blocks of
them, one master range per final-MAC purpose: Stock Only modules keep a
Stock MAC, Stock + Pricol modules a Pricol MAC. The station keeps a
current block and, once it runs low, a reserve block, so the line runs
on while the box is unreachable (SDK guide 4.4-4.5, requirements R5-R10).

The box keeps one current + reserve pair per station, so the station
holds blocks for the mode in use only. After a mode change the first
block of the new range is requested while the box still files the old
pair; the box answers reserve_already_held and the old pair is promoted
(closed) as in guide 4.11. The old blocks' remaining addresses are lost.

Switching to the reserve happens inside the MAC reservation of a tray
(BatchRepository.reserve_macs); this service only tells the box about
it afterwards (promote_block), before it asks for any new block.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy.orm import Session, sessionmaker
from unisemble_client import errors

from stationapp.data.box_block_repository import BlockState, BoxBlockRepository
from stationapp.domain.mac import MacPurpose
from stationapp.drivers.box.gateway import BoxGateway
from stationapp.services.mac_cache import MacCacheStatus

logger = logging.getLogger(__name__)

LABELS = {MacPurpose.STOCK_RF_TEST: "Stock", MacPurpose.PRICOL_PRODUCTION: "Pricol"}


class BoxBlockService:
    """Keep each purpose supplied with a current block and a reserve."""

    def __init__(self, *, session_factory: sessionmaker[Session], gateway: BoxGateway | None, station_id: str, jig_id: str, master_ranges: dict[MacPurpose, str | None], block_size: int, low_water: int) -> None:
        self._repository = BoxBlockRepository(session_factory)
        self._gateway = gateway
        self._station_id = station_id
        self._jig_id = jig_id
        self._master_ranges = master_ranges
        self._block_size = block_size
        self._low_water = low_water
        # One block request at a time: readiness and the report thread
        # may both top up.
        self._lock = threading.Lock()

    @property
    def online(self) -> bool:
        return self._gateway is not None

    def master_range(self, purpose: MacPurpose) -> str | None:
        return self._master_ranges.get(purpose)

    def state(self, purpose: MacPurpose) -> BlockState:
        return self._repository.state(purpose)

    def available(self, purpose: MacPurpose) -> int:
        return self._repository.state(purpose).available

    def ensure(self, purpose: MacPurpose) -> MacCacheStatus:
        """Send owed promotes, then fetch a current or reserve block if
        needed. Never raises for a box problem: production may continue
        on the blocks already held."""
        label = LABELS[purpose]
        problem = ""
        with self._lock:
            if self._gateway is None:
                problem = "box not configured"
            elif not self.master_range(purpose):
                problem = f"no master range configured for {label} MACs"
            else:
                try:
                    self._send_owed_promotes()
                    self._top_up(purpose)
                except errors.UnisembleError as exc:
                    logger.warning("Box block top-up failed | purpose=%s: %s", purpose.value, exc)
                    problem = describe_box_error(exc)

        state = self.state(purpose)
        message = f"{state.available} {label} MACs in box blocks"
        if state.current is not None:
            message += f" (block {state.current.block_id}: {state.current.available} left"
            message += f"; reserve {state.reserve.block_id})" if state.reserve else "; no reserve yet)"
        if problem:
            message += f"; {problem}"
        return MacCacheStatus(purpose=purpose, available=state.available, required=0, message=message)

    def bind_to_box(self, identity: str) -> None:
        """Close blocks issued by another box (see BoxBlockRepository)."""
        closed = self._repository.bind_to_box(identity)
        if closed:
            logger.warning("Box changed to %s: %d block(s) from the previous box closed and never used", identity, closed)

    def send_owed_promotes(self) -> None:
        """Re-send every promote the box has not confirmed (R10)."""
        if self._gateway is None:
            return
        with self._lock:
            try:
                self._send_owed_promotes()
            except errors.UnisembleError as exc:
                logger.warning("Owed promote not sent: %s", exc)

    def top_up_in_use(self) -> None:
        """Background top-up of every purpose already served from blocks."""
        for purpose in MacPurpose:
            if self.state(purpose).current is not None:
                self.ensure(purpose)

    # ------------------------------------------------------------------

    def _send_owed_promotes(self) -> None:
        assert self._gateway is not None
        for old_block_id, new_block_id in self._repository.owed_promotes():
            self._gateway.promote_block(old_block_id, new_block_id)
            self._repository.mark_promoted(new_block_id)
            logger.info("Promote sent | %d -> %d", old_block_id, new_block_id)

    def _top_up(self, purpose: MacPurpose) -> None:
        state = self.state(purpose)
        if state.current is None:
            self._request(purpose)
        elif state.reserve is None and state.current.available <= self._low_water:
            self._request(purpose)

    def _request(self, purpose: MacPurpose) -> None:
        assert self._gateway is not None
        master_range = self.master_range(purpose)
        assert master_range
        try:
            block = self._gateway.request_block(self._block_size, master_range)
        except errors.Refused as exc:
            if exc.reason != "reserve_already_held" or not self._recover(exc, purpose):
                raise
            # Guide 4.11: the box held blocks this station no longer knows
            # about; they are closed, and the request is made again.
            block = self._gateway.request_block(self._block_size, master_range)

        state = self._repository.add(
            block_id=block.block_id,
            master_range=master_range,
            first_mac=block.first_mac,
            last_mac=block.last_mac,
            size=block.size,
            purpose=purpose,
            station_id=self._station_id,
            jig_id=self._jig_id,
        )
        logger.info("Box block received | purpose=%s block=%d %s..%s as %s", purpose.value, block.block_id, block.first_mac, block.last_mac, state)

    def _recover(self, exc: errors.Refused, purpose: MacPurpose) -> bool:
        """Promote a pair the box holds for this station when its open
        block is not one this station programs `purpose` from: blocks it
        lost track of (guide 4.11), or the previous mode's blocks. Never
        closes a block held for `purpose`."""
        assert self._gateway is not None
        state = self.state(purpose)
        held = {view.block_id for view in (state.current, state.reserve) if view is not None}
        if exc.open_block_id is None or exc.reserve_block_id is None or exc.open_block_id in held:
            return False
        logger.warning("Box holds blocks %s/%s unknown to this station; promoting them (guide 4.11)", exc.open_block_id, exc.reserve_block_id)
        self._gateway.promote_block(exc.open_block_id, exc.reserve_block_id)
        return True


def describe_box_error(exc: errors.UnisembleError) -> str:
    """Words for the operator, after the SDK guide."""
    if isinstance(exc, errors.Refused):
        return {
            "master_range_unknown": "the box has no master range of that name - check the configuration",
            "master_range_used_up": "every address in this master range has been handed out - head office must add capacity",
            "station_key_rejected": "the box did not accept the station key - call support",
            "not_allowed": "this account cannot operate a station - call support",
            "reserve_already_held": f"the box already holds a current ({exc.open_block_id}) and a reserve ({exc.reserve_block_id}) block for this station - call support",
            "block_not_found": f"the box does not know that block - call support ({exc.detail})",
        }.get(exc.reason, f"the box refused the request ({exc.reason}) - call support")
    if isinstance(exc, errors.BoxUnreachable):
        return f"the box is not answering ({exc.detail})"
    if isinstance(exc, errors.ClientBusy):
        return "the box connection is busy - try again"
    return str(exc) or type(exc).__name__
