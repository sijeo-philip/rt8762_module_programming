"""MPCLI exit/error codes with plain-language explanations.

Codes and names are from the Realtek MPCLI User Guide, Table 5.1
("Error Code"). The meaning and action texts are for operators and
supervisors at the station.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MpCliErrorCode:
    code: int
    name: str        # as printed in the MPCLI User Guide
    meaning: str     # what it means at the jig
    action: str      # what to do


MPCLI_ERROR_CODES: dict[int, MpCliErrorCode] = {
    item.code: item
    for item in (
        MpCliErrorCode(0, "Success", "The operation completed.", "None."),
        MpCliErrorCode(1, "Parameter error", "MPCLI was given an invalid option or value.", "Supervisor: check the station / bench settings."),
        MpCliErrorCode(2, "Port number error", "The COM port is invalid or could not be used.", "Check the USB-serial adapter and the slot's COM-port binding."),
        MpCliErrorCode(3, "Open file error", "The firmware file could not be opened.", "Supervisor: check the firmware file path and that the file exists."),
        MpCliErrorCode(4, "ic type error", "The chip type does not match the firmware image.", "Check the correct module type is loaded and the correct firmware is selected."),
        MpCliErrorCode(5, "No ready error", "The module did not become ready for programming.", "Reset the module into download mode and retry."),
        MpCliErrorCode(6, "hci event error", "The module did not answer MPCLI's command.", "Check the module is seated, powered and in download mode; check TX/RX wiring."),
        MpCliErrorCode(7, "mp event error", "The module rejected or failed a programming command.", "Retry; if it repeats, set the module aside as faulty."),
        MpCliErrorCode(8, "crc check error", "Data was corrupted during transfer.", "Check the cable/adapter and retry."),
        MpCliErrorCode(9, "Timeout error", "The module stopped responding.", "Check power and connection, reset the module and retry."),
        MpCliErrorCode(10, "Flash read error", "Reading the module's flash memory failed.", "Retry; if it repeats, the module flash may be faulty."),
        MpCliErrorCode(11, "Flash verify error", "The written firmware does not match the file.", "Retry; if it repeats, the module flash is faulty."),
        MpCliErrorCode(12, "Configuration signature error", "MPCLI's configuration file signature is invalid.", "Supervisor: restore the original MPCLI configuration files."),
        MpCliErrorCode(13, "Packed image parse error", "The firmware image file is invalid or damaged.", "Supervisor: select a valid packed firmware image."),
        MpCliErrorCode(14, "make temp dir error", "The PC could not create a temporary folder.", "Check free disk space and folder permissions on the PC."),
        MpCliErrorCode(15, "alloc error", "The PC ran out of memory.", "Close other programs or restart the PC."),
        MpCliErrorCode(16, "save error", "The PC could not save a file.", "Check free disk space and folder permissions on the PC."),
        MpCliErrorCode(17, "delete error", "The PC could not delete a temporary file.", "Close programs using MPCLI files and retry."),
        MpCliErrorCode(18, "Dump data error", "Reading data back from the module failed.", "Check the connection, reset the module and retry."),
    )
}

_ERR = re.compile(r"-ERR:\s*(\d+)-")
_CMD = re.compile(r"CMD\[\d+\]\s+(\S+)")


def lookup(code: int | None) -> MpCliErrorCode | None:
    if code is None:
        return None
    return MPCLI_ERROR_CODES.get(code)


def failed_step(output: str) -> str | None:
    """Name of the MPCLI command that failed (e.g. 'loadfw'), if printed."""
    step = None
    for line in output.splitlines():
        command = _CMD.search(line)
        if command:
            step = command.group(1)
        if _ERR.search(line):
            return step
    return None


def describe(code: int | None, output: str = "") -> str:
    """One-line explanation, e.g. 'MPCLI ERR 6 (hci event error) at loadfw:
    the module did not answer ... -> check the module is seated ...'."""
    if code is None:
        return "MPCLI did not report an exit code."
    entry = lookup(code)
    step = failed_step(output)
    where = f" at {step}" if step else ""
    if entry is None:
        return f"MPCLI ERR {code}{where}: unknown error code (see the MPCLI User Guide)."
    return f"MPCLI ERR {code} ({entry.name}){where}: {entry.meaning} -> {entry.action}"
