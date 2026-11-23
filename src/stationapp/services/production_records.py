"""Read-only search of the local production records (Supervisor tool).

One row per module that went through a tray: when, which tray and slot,
who ran it, its QR, its Stock and Pricol MACs, its Batch Code and its
result. Rows are built from the batches and batch_slots tables and the
append-only manufacturing events (MACs, RF result, Batch Code, retries),
so trays that were aborted or are still running are found as well as
committed ones.

Searches: date range, MAC (full or trailing bytes, any separator),
Operator, module QR and Batch Code; any combination. Nothing here
writes to the database.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from sqlalchemy import Text, cast, distinct, or_, select
from sqlalchemy.orm import Session, sessionmaker

from stationapp.data.models import BatchModel, BatchSlotModel, ManufacturingEventModel, UserModel
from stationapp.domain.events import EventType

DEFAULT_LIMIT = 5000

_HEX = re.compile(r"[^0-9A-Fa-f]")

# Events that carry the per-module facts shown in a record.
_RECORD_EVENTS = (
    EventType.STOCK_MAC_RESERVED.value,
    EventType.PRICOL_MAC_RESERVED.value,
    EventType.BATCH_RESTARTED.value,
    EventType.STOCK_RF_RESULT.value,
    EventType.BATCH_COMMITTED.value,
    EventType.STAGE_RETRY.value,
)


@dataclass(frozen=True, slots=True)
class RecordFilter:
    """Search criteria; empty fields do not filter."""

    date_from: date | None = None        # local calendar dates, inclusive
    date_to: date | None = None
    mac: str = ""
    operator: str = ""
    module_qr: str = ""
    batch_code: str = ""
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True, slots=True)
class ProductionRecord:
    started_at: datetime                 # local time
    committed_at: datetime | None
    batch_id: str
    batch_label: str | None
    operator_id: str | None
    operator_name: str | None
    programming_mode: str
    tray_state: str
    slot_number: int
    module_qr: str | None
    stock_mac: str | None
    pricol_mac: str | None
    batch_code: str | None
    result: str
    rf_outcome: str | None
    app_test: bool | None
    dfu_test: bool | None
    retries: int
    hold_reason: str | None


@dataclass(frozen=True, slots=True)
class RecordSearchResult:
    records: tuple[ProductionRecord, ...]
    truncated: bool


# Column headings for tables and CSV, in ProductionRecord order.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("started_at", "Date / time"),
    ("committed_at", "Committed"),
    ("batch_id", "Tray ID"),
    ("batch_label", "Lot"),
    ("operator_id", "Operator"),
    ("operator_name", "Operator name"),
    ("programming_mode", "Mode"),
    ("tray_state", "Tray state"),
    ("slot_number", "Slot"),
    ("module_qr", "Module QR"),
    ("stock_mac", "Stock MAC"),
    ("pricol_mac", "Pricol MAC"),
    ("batch_code", "Batch Code"),
    ("result", "Result"),
    ("rf_outcome", "RF"),
    ("app_test", "APP test"),
    ("dfu_test", "DFU test"),
    ("retries", "Retries"),
    ("hold_reason", "Failure / hold reason"),
)


def normalise_mac(text: str) -> str:
    """Hex digits only, upper case ('aa:bb-cc' -> 'AABBCC')."""
    return _HEX.sub("", text or "").upper()


def _compact(mac: str | None) -> str:
    return normalise_mac(mac or "")


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_local_tz())


def _utc_bound(day: date, *, end: bool) -> datetime:
    """Naive UTC instant of the start of a local day (or of the next day)."""
    local = datetime.combine(day + timedelta(days=1) if end else day, time.min).replace(tzinfo=_local_tz())
    return local.astimezone(timezone.utc).replace(tzinfo=None)


class ProductionRecordService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------

    def operators(self) -> tuple[str, ...]:
        """Operator IDs that appear on trays, for the search list."""
        with self._session_factory() as session:
            rows = session.scalars(
                select(distinct(BatchModel.operator_id)).where(BatchModel.operator_id.is_not(None)).order_by(BatchModel.operator_id)
            ).all()
        return tuple(row for row in rows if row)

    def search(self, criteria: RecordFilter) -> RecordSearchResult:
        mac = normalise_mac(criteria.mac)
        operator = criteria.operator.strip()
        qr = criteria.module_qr.strip()
        code = criteria.batch_code.strip()
        limit = max(1, criteria.limit)

        with self._session_factory() as session:
            query = (
                select(BatchModel, BatchSlotModel)
                .join(BatchSlotModel, BatchSlotModel.batch_id == BatchModel.batch_id)
                .order_by(BatchModel.created_at.desc(), BatchModel.batch_id, BatchSlotModel.slot_number)
            )
            if criteria.date_from is not None:
                query = query.where(BatchModel.created_at >= _utc_bound(criteria.date_from, end=False))
            if criteria.date_to is not None:
                query = query.where(BatchModel.created_at < _utc_bound(criteria.date_to, end=True))
            if operator:
                names = select(UserModel.user_id).where(UserModel.display_name.ilike(f"%{operator}%"))
                query = query.where(or_(BatchModel.operator_id.ilike(f"%{operator}%"), BatchModel.operator_id.in_(names)))
            if qr:
                query = query.where(BatchSlotModel.module_qr.ilike(f"%{qr}%"))

            # MACs and Batch Codes live in event payloads: narrow to the
            # trays whose events mention them, then match exactly below.
            if mac and len(mac) % 2 == 0:
                colon = ":".join(mac[index:index + 2] for index in range(0, len(mac), 2))
                query = query.where(BatchModel.batch_id.in_(self._batches_mentioning(colon)))
            if code:
                query = query.where(BatchModel.batch_id.in_(self._batches_mentioning(code, EventType.BATCH_COMMITTED.value)))

            # Module slots only: a slot never loaded has no record.
            query = query.where(or_(BatchSlotModel.device_state != "EMPTY", BatchSlotModel.module_qr.is_not(None)))

            rows = session.execute(query).all()
            batch_ids = list(dict.fromkeys(batch.batch_id for batch, _slot in rows))
            facts = self._facts(session, batch_ids)
            names = dict(session.execute(select(UserModel.user_id, UserModel.display_name)).all())

        records: list[ProductionRecord] = []
        truncated = False
        for batch, slot in rows:
            fact = facts.get((batch.batch_id, slot.slot_number), {})
            record = ProductionRecord(
                started_at=_to_local(batch.created_at),
                committed_at=_to_local(batch.committed_at),
                batch_id=batch.batch_id,
                batch_label=batch.batch_label,
                operator_id=batch.operator_id,
                operator_name=names.get(batch.operator_id) if batch.operator_id else None,
                programming_mode=batch.programming_mode,
                tray_state=batch.state,
                slot_number=slot.slot_number,
                module_qr=slot.module_qr,
                stock_mac=fact.get("stock_mac"),
                pricol_mac=fact.get("pricol_mac"),
                batch_code=fact.get("batch_code"),
                result=fact.get("disposition") or slot.device_state,
                rf_outcome=fact.get("rf_outcome"),
                app_test=slot.pricol_app_passed,
                dfu_test=slot.pricol_dfu_passed,
                retries=fact.get("retries", 0),
                hold_reason=slot.hold_reason,
            )
            if mac and mac not in _compact(record.stock_mac) and mac not in _compact(record.pricol_mac):
                continue
            if code and (record.batch_code or "").upper() != code.upper():
                continue
            if len(records) >= limit:
                truncated = True
                break
            records.append(record)
        return RecordSearchResult(records=tuple(records), truncated=truncated)

    # ------------------------------------------------------------------

    @staticmethod
    def _batches_mentioning(text: str, event_type: str | None = None):
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = select(ManufacturingEventModel.batch_id).where(
            ManufacturingEventModel.batch_id.is_not(None),
            cast(ManufacturingEventModel.payload, Text).ilike(f"%{escaped}%", escape="\\"),
        )
        if event_type is not None:
            query = query.where(ManufacturingEventModel.event_type == event_type)
        return query

    @staticmethod
    def _facts(session: Session, batch_ids: Sequence[str]) -> dict[tuple[str, int], dict]:
        """Per (batch, slot): latest MACs, RF outcome, Batch Code, result, retries."""
        facts: dict[tuple[str, int], dict] = {}
        for start in range(0, len(batch_ids), 500):
            chunk = batch_ids[start:start + 500]
            events = session.execute(
                select(
                    ManufacturingEventModel.batch_id,
                    ManufacturingEventModel.slot_number,
                    ManufacturingEventModel.event_type,
                    ManufacturingEventModel.payload,
                )
                .where(ManufacturingEventModel.batch_id.in_(chunk), ManufacturingEventModel.event_type.in_(_RECORD_EVENTS))
                .order_by(ManufacturingEventModel.occurred_at, ManufacturingEventModel.id)
            ).all()
            for batch_id, slot_number, event_type, stored in events:
                payload = _event_data(stored)
                if event_type == EventType.BATCH_RESTARTED.value:
                    for key, value in (payload.get("stock_macs") or {}).items():
                        facts.setdefault((batch_id, int(key)), {})["stock_mac"] = value
                    for key, value in (payload.get("pricol_macs") or {}).items():
                        facts.setdefault((batch_id, int(key)), {})["pricol_mac"] = value
                    for (bid, _slot), fact in facts.items():
                        if bid == batch_id:
                            fact.pop("rf_outcome", None)
                    continue
                if event_type == EventType.BATCH_COMMITTED.value:
                    for key, value in (payload.get("batch_codes") or {}).items():
                        facts.setdefault((batch_id, int(key)), {})["batch_code"] = value
                    for key, value in (payload.get("dispositions") or {}).items():
                        facts.setdefault((batch_id, int(key)), {})["disposition"] = value
                    continue
                if slot_number is None:
                    continue
                fact = facts.setdefault((batch_id, slot_number), {})
                if event_type == EventType.STOCK_MAC_RESERVED.value:
                    fact["stock_mac"] = payload.get("mac")
                elif event_type == EventType.PRICOL_MAC_RESERVED.value:
                    fact["pricol_mac"] = payload.get("mac")
                elif event_type == EventType.STOCK_RF_RESULT.value:
                    fact["rf_outcome"] = payload.get("outcome")
                elif event_type == EventType.STAGE_RETRY.value:
                    fact["retries"] = fact.get("retries", 0) + 1
        return facts


def _event_data(stored) -> dict:
    """The event's own payload (ManufacturingEventRepository stores it
    under "data", next to a "context" envelope)."""
    if not isinstance(stored, dict):
        return {}
    data = stored.get("data")
    return data if isinstance(data, dict) else {}


def format_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def export_csv(records: Iterable[ProductionRecord], path: Path) -> int:
    """Write records to a CSV file (Excel-friendly UTF-8); returns the row count."""
    names = [field.name for field in fields(ProductionRecord)]
    headings = dict(COLUMNS)
    count = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([headings.get(name, name) for name in names])
        for record in records:
            writer.writerow([format_value(getattr(record, name)) for name in names])
            count += 1
    return count
