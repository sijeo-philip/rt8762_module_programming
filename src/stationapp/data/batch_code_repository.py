"""The running Batch Code, stored as a station setting.

Session-bound: flushes, never commits. The tray commit writes it in the
same transaction as the tray and its report, so a power cut can never
count a tray that is not queued for the box, or the other way round.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from stationapp.data.models import StationSettingModel
from stationapp.domain.batch_code import BatchCodeState

KEY = "batch_code"


def load_batch_code(session: Session) -> BatchCodeState | None:
    row = session.get(StationSettingModel, KEY)
    if row is None or not row.value:
        return None
    return BatchCodeState.from_dict(row.value)


def save_batch_code(session: Session, state: BatchCodeState, updated_by: str) -> None:
    row = session.get(StationSettingModel, KEY)
    now = datetime.now(timezone.utc)
    if row is None:
        session.add(StationSettingModel(key=KEY, value=state.to_dict(), updated_at=now, updated_by=updated_by))
    else:
        row.value, row.updated_at, row.updated_by = state.to_dict(), now, updated_by
    session.flush()
