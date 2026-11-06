"""Box blocks in the local database (docs/box_integration.md).

A block from the box is saved, with every one of its addresses as an
AVAILABLE row of the authorized MAC cache, in one transaction. Batches
then reserve addresses from the CURRENT block only, bottom up; when the
current block cannot fill a tray, BatchRepository.reserve_macs switches
to the RESERVE block, closes the old one and records the owed promote
in the same transaction that reserves the tray's addresses.

The box keeps one current and one reserve block per station, whatever
the master range. Blocks are held for one purpose at a time: the first
block saved for the other purpose (the mode was changed) closes every
open block of the previous one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.errors import DuplicateMacInDatabase
from stationapp.data.models import AllocatedMacModel, AllocationModel, BoxBlockModel, StationSettingModel
from stationapp.domain.mac import MacAddress, MacPurpose, MacStatus

CURRENT = "CURRENT"
RESERVE = "RESERVE"
CLOSED = "CLOSED"
BOX_IDENTITY_KEY = "box_identity"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def box_allocation_id(block_id: int) -> str:
    return f"BOX-{block_id}"


class BoxBlockConflict(Exception):
    """A block cannot be saved: the station already holds a reserve, or
    the box handed out an address this station already has."""


@dataclass(frozen=True, slots=True)
class BlockView:
    block_id: int
    master_range: str
    first_mac: MacAddress
    last_mac: MacAddress
    size: int
    available: int


@dataclass(frozen=True, slots=True)
class BlockState:
    """What the station holds for one purpose."""

    purpose: MacPurpose
    current: BlockView | None
    reserve: BlockView | None
    owed_promote: tuple[int, int] | None    # (old block, new block)

    @property
    def available(self) -> int:
        return sum(block.available for block in (self.current, self.reserve) if block is not None)


class BoxBlockRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def add(self, *, block_id: int, master_range: str, first_mac: str, last_mac: str, size: int, purpose: MacPurpose, station_id: str, jig_id: str) -> str:
        """Save a new block as CURRENT, or as RESERVE when a current block
        is held, and close the open blocks of every other purpose.
        Returns the state it was saved in."""
        first, last = MacAddress.parse(first_mac), MacAddress.parse(last_mac)
        if last.value - first.value + 1 != size or size <= 0:
            raise BoxBlockConflict(f"Block {block_id}: {first}..{last} does not hold {size} addresses")

        session = self._session_factory()
        try:
            session.execute(text("BEGIN IMMEDIATE"))
            held = {row.state for row in self._open(session, purpose)}
            if RESERVE in held:
                raise BoxBlockConflict(f"A reserve {purpose.value} block is already held")
            state = RESERVE if CURRENT in held else CURRENT

            overlap = session.scalar(
                select(func.count(AllocatedMacModel.id)).where(
                    AllocatedMacModel.address.between(first.compact, last.compact)
                )
            )
            if overlap:
                raise DuplicateMacInDatabase(f"Block {block_id} overlaps {overlap} address(es) already held by this station")

            now = utc_now()
            for row in session.scalars(select(BoxBlockModel).where(BoxBlockModel.purpose != purpose.value, BoxBlockModel.state != CLOSED)):
                row.state, row.closed_at = CLOSED, now
            allocation_id = box_allocation_id(block_id)
            session.add(AllocationModel(
                allocation_id=allocation_id,
                station_id=station_id,
                jig_id=jig_id,
                purpose=purpose.value,
                issued_at=now,
                schema_version=1,
                document_digest=f"box-block:{block_id}",
            ))
            session.flush()
            session.execute(insert(AllocatedMacModel), [
                {"allocation_id": allocation_id, "address": f"{value:012X}", "purpose": purpose.value, "status": MacStatus.AVAILABLE.value}
                for value in range(first.value, last.value + 1)
            ])
            session.add(BoxBlockModel(
                block_id=block_id,
                allocation_id=allocation_id,
                purpose=purpose.value,
                master_range=master_range,
                first_mac=first.compact,
                last_mac=last.compact,
                size=size,
                state=state,
                received_at=now,
            ))
            session.commit()
            return state
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def state(self, purpose: MacPurpose) -> BlockState:
        with self._session_factory() as session:
            current = reserve = None
            for row in self._open(session, purpose):
                view = self._view(session, row)
                if row.state == CURRENT:
                    current = view
                else:
                    reserve = view
            owed = session.scalar(self._owed().where(BoxBlockModel.purpose == purpose.value))
            return BlockState(
                purpose=purpose,
                current=current,
                reserve=reserve,
                owed_promote=(owed.promote_from_block_id, owed.block_id) if owed is not None else None,
            )

    def owed_promotes(self) -> tuple[tuple[int, int], ...]:
        with self._session_factory() as session:
            return tuple((row.promote_from_block_id, row.block_id) for row in session.scalars(self._owed().order_by(BoxBlockModel.block_id)))

    def mark_promoted(self, new_block_id: int) -> None:
        with self._session_factory() as session:
            row = session.get(BoxBlockModel, new_block_id)
            if row is not None and row.promoted_at is None:
                row.promoted_at = utc_now()
                session.commit()

    def close_other_ranges(self, purpose: MacPurpose, master_range: str) -> tuple[int, ...]:
        """Close the open blocks of `purpose` drawn from any master range
        other than `master_range` (the Supervisor changed the range), so
        no further address comes from the old range (R7). Addresses a
        tray has already reserved stay with that tray. An owed promote
        is kept: the box still expects it. Returns the closed block ids."""
        with self._session_factory() as session:
            now = utc_now()
            closed = []
            for row in self._open(session, purpose):
                if row.master_range != master_range:
                    row.state, row.closed_at = CLOSED, now
                    closed.append(row.block_id)
            session.commit()
            return tuple(closed)

    def bind_to_box(self, identity: str) -> int:
        """Blocks belong to the box that issued them. When the station
        now talks to a different box (demo box -> real box, a replaced
        box), every open block is closed and never used again, and owed
        promotes are dropped. Returns the number of blocks closed."""
        with self._session_factory() as session:
            setting = session.get(StationSettingModel, BOX_IDENTITY_KEY)
            if setting is not None and setting.value.get("box") == identity:
                return 0
            now = utc_now()
            closed = 0
            for row in session.scalars(select(BoxBlockModel).where(BoxBlockModel.state != CLOSED)):
                row.state, row.closed_at = CLOSED, now
                closed += 1
            for row in session.scalars(self._owed()):
                row.promoted_at = now   # not owed to the new box
            if setting is None:
                session.add(StationSettingModel(key=BOX_IDENTITY_KEY, value={"box": identity}, updated_at=now, updated_by="SYSTEM"))
            else:
                setting.value, setting.updated_at, setting.updated_by = {"box": identity}, now, "SYSTEM"
            session.commit()
            return closed

    # ------------------------------------------------------------------

    @staticmethod
    def _open(session: Session, purpose: MacPurpose) -> list[BoxBlockModel]:
        return list(session.scalars(
            select(BoxBlockModel).where(BoxBlockModel.purpose == purpose.value, BoxBlockModel.state != CLOSED)
        ))

    @staticmethod
    def _owed():
        return select(BoxBlockModel).where(BoxBlockModel.promote_from_block_id.is_not(None), BoxBlockModel.promoted_at.is_(None))

    @staticmethod
    def _view(session: Session, row: BoxBlockModel) -> BlockView:
        available = session.scalar(
            select(func.count(AllocatedMacModel.id)).where(
                AllocatedMacModel.allocation_id == row.allocation_id,
                AllocatedMacModel.status == MacStatus.AVAILABLE.value,
            )
        )
        return BlockView(
            block_id=row.block_id,
            master_range=row.master_range,
            first_mac=MacAddress.parse(row.first_mac),
            last_mac=MacAddress.parse(row.last_mac),
            size=row.size,
            available=int(available or 0),
        )


def current_block_addresses(session: Session, purpose: MacPurpose, count: int) -> list[AllocatedMacModel] | None:
    """The lowest `count` AVAILABLE addresses of the current box block.

    When the current block cannot fill the request and a reserve block
    is held, the reserve becomes current, the old block is closed and
    the promote is recorded as owed - all in the caller's transaction, so
    the switch is saved together with the reservation or not at all. A
    request is never split across two blocks.

    None when this purpose has never been served from box blocks.
    """
    rows = list(session.scalars(select(BoxBlockModel).where(BoxBlockModel.purpose == purpose.value)))
    if not rows:
        return None
    current = next((row for row in rows if row.state == CURRENT), None)
    reserve = next((row for row in rows if row.state == RESERVE), None)
    if current is None:
        return []

    available = _lowest_available(session, current.allocation_id, count)
    if len(available) < count and reserve is not None:
        current.state, current.closed_at = CLOSED, utc_now()
        reserve.state, reserve.promote_from_block_id = CURRENT, current.block_id
        session.flush()
        available = _lowest_available(session, reserve.allocation_id, count)
    return available


def _lowest_available(session: Session, allocation_id: str, count: int) -> list[AllocatedMacModel]:
    return list(session.scalars(
        select(AllocatedMacModel)
        .where(AllocatedMacModel.allocation_id == allocation_id, AllocatedMacModel.status == MacStatus.AVAILABLE.value)
        .order_by(AllocatedMacModel.id)
        .limit(count)
    ))


def closed_block_allocations():
    """Allocation ids whose remaining addresses must never be used."""
    return select(BoxBlockModel.allocation_id).where(BoxBlockModel.state == CLOSED)
