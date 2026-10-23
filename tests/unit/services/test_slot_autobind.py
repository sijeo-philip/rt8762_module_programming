"""Slot binding from reference modules: matching rules."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stationapp.services.slot_autobind import (
    PortReading,
    build_plan,
    normalise_prefix,
    read_reference_modules,
    reference_slot,
)


def reading(device, mac=None, error=None):
    return PortReading(device, f"USB:{device}", mac=mac, error=error)


@pytest.mark.unit
def test_reference_mac_names_the_slot():
    assert reference_slot("22:22:22:22:22:01") == 1
    assert reference_slot("222222222208") == 8
    assert reference_slot("22:22:22:22:22:00") is None          # no slot 0
    assert reference_slot("02:11:11:11:11:F1") is None          # not a reference module
    assert reference_slot("AA:BB:CC:DD:EE:03", "aa-bb-cc-dd-ee") == 3
    with pytest.raises(ValueError):
        normalise_prefix("22:22:22")


@pytest.mark.unit
def test_ports_bind_to_the_slot_their_module_names_whatever_the_com_order():
    plan = build_plan(
        [
            reading("COM4", "22:22:22:22:22:03"),
            reading("COM6", "22:22:22:22:22:01"),
            reading("COM7", "22:22:22:22:22:04"),
            reading("COM8", "22:22:22:22:22:02"),
        ],
        jig_positions=4,
    )
    assert {slot: item.device for slot, item in plan.assignments.items()} == {1: "COM6", 2: "COM8", 3: "COM4", 4: "COM7"}
    assert plan.complete and plan.summary() == "4/4 slots identified"


@pytest.mark.unit
def test_problems_are_reported_never_guessed():
    plan = build_plan(
        [
            reading("COM3", "22:22:22:22:22:01"),
            reading("COM4", None, error="MPCLI ERR 6"),              # no module / not in boot mode
            reading("COM5", "02:11:11:11:11:F1"),                    # a production module
            reading("COM6", "22:22:22:22:22:02"),
            reading("COM7", "22:22:22:22:22:02"),                    # two modules claim slot 2
            reading("COM9", "22:22:22:22:22:09"),                    # beyond an 8-slot jig
        ],
        jig_positions=8,
    )
    assert set(plan.assignments) == {1}
    assert plan.missing_slots == (2, 3, 4, 5, 6, 7, 8)
    text = "\n".join(plan.problems)
    assert "COM4: no MAC read (MPCLI ERR 6)" in text
    assert "COM5: MAC 02:11:11:11:11:F1 is not a reference MAC" in text
    assert "Slot 2: reference MAC read on COM6 and COM7" in text
    assert "names slot 9, but the jig has 8 slots" in text
    assert not plan.complete


@pytest.mark.unit
def test_all_ports_are_read_and_failures_do_not_stop_the_others():
    macs = {"COM10": "22:22:22:22:22:02", "COM11": "22:22:22:22:22:01"}

    def read_mac(device):
        if device == "COM12":
            raise OSError("port busy")
        return SimpleNamespace(mac=macs.get(device), message="MAC not found")

    plan = read_reference_modules([("COM10", "K10"), ("COM11", "K11"), ("COM12", "K12")], read_mac, jig_positions=4)
    assert {slot: item.stable_key for slot, item in plan.assignments.items()} == {1: "K11", 2: "K10"}
    assert any("COM12" in problem and "port busy" in problem for problem in plan.problems)
