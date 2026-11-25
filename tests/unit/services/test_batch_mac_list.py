from __future__ import annotations

from datetime import datetime

import pytest
from openpyxl import load_workbook

from stationapp.services.batch_mac_list import BatchMacList, BatchMacListService, table_shape, write_workbook
from stationapp.services.production_records import ProductionRecord, RecordSearchResult


def record(slot, *, committed, mode="STOCK_AND_PRICOL", result="PASS", stock="AA:00:00:00:00:01", pricol="bb-00-00-00-00-01"):
    return ProductionRecord(
        started_at=committed, committed_at=committed, batch_id=f"T-{committed:%H%M}", batch_label=None,
        operator_id="OP", operator_name=None, programming_mode=mode, tray_state="COMMITTED", slot_number=slot,
        module_qr=f"Q{slot}", stock_mac=stock, pricol_mac=pricol, batch_code="S5A01", result=result,
        rf_outcome=None, app_test=None, dfu_test=None, retries=0, hold_reason=None,
    )


class FakeRecords:
    def __init__(self, records):
        self.records = records
        self.criteria = None

    def search(self, criteria):
        self.criteria = criteria
        return RecordSearchResult(records=tuple(self.records), truncated=False)


@pytest.mark.unit
def test_lists_final_macs_of_passed_modules_in_commit_and_slot_order(tmp_path):
    early, late = datetime(2026, 11, 23, 9, 0), datetime(2026, 11, 25, 17, 0)
    records = FakeRecords([
        record(2, committed=late, pricol="BB:00:00:00:00:04"),
        record(1, committed=late, pricol="BB:00:00:00:00:03"),
        record(3, committed=late, result="FAIL", pricol="BB:00:00:00:00:09"),
        record(1, committed=early, mode="STOCK_ONLY", stock="aa:00:00:00:00:01", pricol=None),
    ])
    mac_list = BatchMacListService(records, output_dir=tmp_path).load(" S5A01 ")

    assert records.criteria.batch_code == "S5A01"
    assert mac_list.macs == ("AA0000000001", "BB0000000003", "BB0000000004")
    assert mac_list.completed_at == late and mac_list.week == 48


@pytest.mark.unit
def test_no_passed_modules_is_refused(tmp_path):
    service = BatchMacListService(FakeRecords([]), output_dir=tmp_path)
    with pytest.raises(ValueError, match="No passed modules"):
        service.export("S5A01")


@pytest.mark.unit
@pytest.mark.parametrize(("count", "shape"), [(1000, (50, 20)), (999, (50, 20)), (100, (50, 2)), (7, (7, 1)), (2000, (100, 20)), (0, (0, 0))])
def test_table_shape(count, shape):
    assert table_shape(count) == shape


@pytest.mark.unit
def test_workbook_is_one_a4_landscape_page_with_editable_header(tmp_path):
    macs = tuple(f"C0FFEE{n:06X}" for n in range(1000))
    path = tmp_path / "list.xlsx"
    write_workbook(BatchMacList("S5A01", macs, datetime(2026, 11, 25, 15, 0), "ST-01"), path)

    sheet = load_workbook(path).active
    assert (sheet["A2"].value, sheet["C2"].value) == ("Batch/Lot No:", "S5A01")
    assert (sheet["H2"].value, sheet["J2"].value) == ("Week No:", 48)
    assert sheet["N2"].value == 1000 and sheet["R2"].value == "25-11-2026"
    assert (sheet["A4"].value, sheet["T4"].value) == ("1-50", "951-1000")
    # Filled column by column: 50 down, then the next column.
    assert (sheet["A5"].value, sheet["A54"].value, sheet["B5"].value) == (macs[0], macs[49], macs[50])
    assert sheet["T54"].value == macs[999]

    setup = sheet.page_setup
    assert setup.orientation == "landscape" and int(setup.paperSize) == 9  # A4
    assert sheet.sheet_properties.pageSetUpPr.fitToPage
    assert (setup.fitToWidth, setup.fitToHeight) == (1, 1)
    assert sheet.print_area == "'MAC List'!$A$1:$T$54"
