"""Printable MAC ID list of one Batch Code (Batch/Lot).

When a Batch Code is complete the Operator prints the MAC IDs of its
passed modules. The list is an Excel workbook laid out for one A4
landscape page: a header with the Batch/Lot No, Week No, quantity and
date, then the MACs in a table filled column by column (50 per column,
20 columns for 1000 modules). The header cells are ordinary cells, so
the Operator can fill in or correct them in Excel before printing.

The MAC printed is the module's final MAC, the one reported to the box:
the Pricol MAC in Stock + Pricol mode, otherwise the Stock MAC, in
compact form (12 hex digits). Only passed modules of committed trays
carry a Batch Code, so only they are listed, in commit and slot order.

Nothing here writes to the database.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from stationapp.services.production_records import ProductionRecordService, RecordFilter, normalise_mac

logger = logging.getLogger(__name__)

ROWS_PER_COLUMN = 50          # 1000 MACs -> 50 rows x 20 columns
COLUMNS_PER_PAGE = 20
_MAX_RECORDS = 1_000_000
_UNSAFE_FILENAME = re.compile(r'[^A-Za-z0-9._-]+')


@dataclass(frozen=True, slots=True)
class BatchMacList:
    batch_code: str
    macs: tuple[str, ...]                # compact, upper case, production order
    completed_at: datetime | None        # local time the last listed tray was committed
    station_id: str = ""

    @property
    def week(self) -> int | None:
        """ISO week number of the completion date."""
        return self.completed_at.isocalendar().week if self.completed_at else None


def table_shape(count: int) -> tuple[int, int]:
    """(rows, columns) of the MAC table: 50 rows a column, and more rows
    rather than more than 20 columns once the list passes 1000."""
    if count <= 0:
        return 0, 0
    rows = max(min(count, ROWS_PER_COLUMN), math.ceil(count / COLUMNS_PER_PAGE))
    return rows, math.ceil(count / rows)


class BatchMacListService:
    def __init__(self, records: ProductionRecordService, *, output_dir: Path, station_id: str = "") -> None:
        self._records = records
        self._output_dir = Path(output_dir)
        self._station_id = station_id

    def load(self, batch_code: str) -> BatchMacList:
        code = (batch_code or "").strip()
        if not code:
            raise ValueError("Enter the Batch Code")
        result = self._records.search(RecordFilter(batch_code=code, limit=_MAX_RECORDS))
        passed = [record for record in result.records if record.result == "PASS"]
        passed.sort(key=lambda r: (r.committed_at or r.started_at, r.started_at, r.batch_id, r.slot_number))
        macs = []
        for record in passed:
            mac = record.pricol_mac if record.programming_mode == "STOCK_AND_PRICOL" else record.stock_mac
            if mac:
                macs.append(normalise_mac(mac))
        completed = max((r.committed_at for r in passed if r.committed_at), default=None)
        return BatchMacList(batch_code=code, macs=tuple(macs), completed_at=completed, station_id=self._station_id)

    def export(self, batch_code: str) -> tuple[Path, BatchMacList]:
        """Write the Batch Code's workbook; returns its path and the list.

        A new file every time, so a copy still open in Excel never blocks it."""
        mac_list = self.load(batch_code)
        if not mac_list.macs:
            raise ValueError(f"No passed modules found with Batch Code {mac_list.batch_code}")
        self._output_dir.mkdir(parents=True, exist_ok=True)
        safe = _UNSAFE_FILENAME.sub("_", mac_list.batch_code).strip("_") or "batch"
        path = self._output_dir / f"MAC_List_{safe}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        write_workbook(mac_list, path)
        logger.info("Batch MAC list written | batch_code=%s macs=%d path=%s", mac_list.batch_code, len(mac_list.macs), path)
        return path, mac_list


def write_workbook(mac_list: BatchMacList, path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties

    rows, columns = table_shape(len(mac_list.macs))
    width = max(columns, COLUMNS_PER_PAGE)       # the header needs 20 columns
    last = get_column_letter(width)

    book = Workbook()
    sheet = book.active
    sheet.title = "MAC List"

    thin = Side(style="thin", color="000000")
    medium = Side(style="medium", color="000000")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    value_box = Border(left=medium, right=medium, top=medium, bottom=medium)
    centre = Alignment(horizontal="center", vertical="center")
    left = Alignment(horizontal="left", vertical="center")
    right = Alignment(horizontal="right", vertical="center")

    # -- Title --------------------------------------------------------------
    sheet.merge_cells(f"A1:{last}1")
    sheet["A1"] = "MAC ID LIST"
    sheet["A1"].font = Font(name="Arial", size=14, bold=True)
    sheet["A1"].alignment = centre
    sheet.row_dimensions[1].height = 22

    # -- Header: label / value pairs the Operator may edit --------------------
    completed = mac_list.completed_at
    header = (
        ("Batch/Lot No:", 1, 2, mac_list.batch_code, 3, 7),
        ("Week No:", 8, 9, mac_list.week, 10, 11),
        ("Quantity:", 12, 13, len(mac_list.macs), 14, 15),
        ("Date:", 16, 17, completed.strftime("%d-%m-%Y") if completed else None, 18, 20),
    )
    for label, label_from, label_to, value, value_from, value_to in header:
        label_cell = sheet.cell(row=2, column=label_from, value=label)
        label_cell.font = Font(name="Arial", size=11, bold=True)
        label_cell.alignment = right
        sheet.merge_cells(start_row=2, start_column=label_from, end_row=2, end_column=label_to)
        value_cell = sheet.cell(row=2, column=value_from, value=value)
        value_cell.font = Font(name="Arial", size=12, bold=True)
        value_cell.alignment = left
        for column in range(value_from, value_to + 1):
            sheet.cell(row=2, column=column).border = value_box
        sheet.merge_cells(start_row=2, start_column=value_from, end_row=2, end_column=value_to)
    sheet.row_dimensions[2].height = 22
    sheet.row_dimensions[3].height = 6

    # -- MAC table: column by column, 'From-To' over each column -------------
    heading_fill = PatternFill("solid", fgColor="D9D9D9")
    for column in range(1, columns + 1):
        first = (column - 1) * rows + 1
        last_index = min(column * rows, len(mac_list.macs))
        cell = sheet.cell(row=4, column=column, value=f"{first}-{last_index}")
        cell.font = Font(name="Arial", size=8, bold=True)
        cell.alignment = centre
        cell.fill = heading_fill
        cell.border = box
    mac_font = Font(name="Arial Narrow", size=9)
    for index, mac in enumerate(mac_list.macs):
        column, row = divmod(index, rows)
        cell = sheet.cell(row=5 + row, column=1 + column, value=mac)
        cell.font = mac_font
        cell.alignment = centre
        cell.number_format = "@"
    for column in range(1, columns + 1):            # borders on empty tail cells too
        for row in range(rows):
            sheet.cell(row=5 + row, column=column).border = box
    for row in range(4, 5 + rows):
        sheet.row_dimensions[row].height = 11.5
    for column in range(1, width + 1):
        sheet.column_dimensions[get_column_letter(column)].width = 10.5

    # -- Print: A4 landscape, everything on one page ---------------------------
    last_row = 4 + rows
    sheet.print_area = f"A1:{last}{last_row}"
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 1
    sheet.print_options.horizontalCentered = True
    margins = sheet.page_margins
    margins.left = margins.right = 0.25
    margins.top = margins.bottom = 0.3
    margins.header = margins.footer = 0.15
    station = f"Station {mac_list.station_id}  " if mac_list.station_id else ""
    sheet.oddFooter.left.text = f"{station}Printed &D &T"
    sheet.oddFooter.left.size = 7
    sheet.oddFooter.right.text = "Page &P of &N"
    sheet.oddFooter.right.size = 7
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A5"
    book.save(path)


def open_document(path: Path) -> None:
    """Open the workbook in its default application (Excel) to print it."""
    if os.name == "nt":
        os.startfile(str(path))  # noqa: S606 - our own generated file
        return
    import subprocess
    subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
