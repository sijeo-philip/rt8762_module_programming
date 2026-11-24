"""Supervisor 'Production records' page renders and filters records."""

from __future__ import annotations

from datetime import datetime

from stationapp.services.production_records import ProductionRecord, RecordSearchResult
from stationapp.ui.widgets.production_records_page import RANGE_ALL, ProductionRecordsPage


def record(slot: int, qr: str, result: str, retries: int = 0) -> ProductionRecord:
    return ProductionRecord(
        started_at=datetime(2026, 11, 23, 10, 0, 0),
        committed_at=None,
        batch_id="B-1",
        batch_label="LOT-7",
        operator_id="OP-ANU",
        operator_name="Anu",
        programming_mode="STOCK_AND_PRICOL",
        tray_state="UPLOAD_PENDING",
        slot_number=slot,
        module_qr=qr,
        stock_mac="0A:FF:00:00:00:01",
        pricol_mac="02:5B:00:00:00:10",
        batch_code="S5A01",
        result=result,
        rf_outcome="PASS",
        app_test=True,
        dfu_test=True,
        retries=retries,
        hold_reason=None,
    )


class FakeRecords:
    def __init__(self) -> None:
        self.criteria = []

    def operators(self):
        return ("OP-ANU",)

    def search(self, criteria):
        self.criteria.append(criteria)
        return RecordSearchResult(records=(record(1, "QR1", "PASS"), record(2, "QR2", "FAIL", retries=2)), truncated=False)


def test_page_searches_with_filters_and_lists_records(qtbot):
    records = FakeRecords()
    page = ProductionRecordsPage(records)
    qtbot.addWidget(page)
    page.show()

    assert page.table.rowCount() == 2          # first search on show
    assert "2 module(s) on 1 tray(s)" in page.summary.text()
    assert "1 module(s) were retried by the operator" in page.summary.text()
    assert page.export_button.isEnabled()

    page.range_input.setCurrentText(RANGE_ALL)
    page.mac_input.setText("12:34:56")
    page.qr_input.setText("QR2")
    page.code_input.setText("S5A01")
    page.operator_input.setCurrentText("OP-ANU")
    page.search_button.click()

    criteria = records.criteria[-1]
    assert criteria.date_from is None and criteria.date_to is None
    assert (criteria.mac, criteria.module_qr, criteria.batch_code, criteria.operator) == ("12:34:56", "QR2", "S5A01", "OP-ANU")
