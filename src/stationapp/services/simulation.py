"""Simulated station hardware for development and training.

Enabled with SIMULATION_MODE=true. Each simulator presents exactly the
surface of the real service it replaces, so the production controller
runs the same code path with or without a jig attached.

Failure injection (SIMULATION_FAILURES) lets a developer rehearse the
per-slot failure handling the README requires, e.g.

    SIMULATION_FAILURES=stock_program:3,rf:5,pricol_readback:2,app:7

Simulated MAC allocations use the locally administered 02:5A / 02:5B
prefixes and must never be programmed into real modules.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from stationapp.concurrency.cancellation import CancellationToken
from stationapp.domain.allocation import AllocationDocument
from stationapp.domain.golden_rig import (
    GoldenRigRequest,
    GoldenRigResponse,
    GoldenRigSlotResult,
    GoldenRigTarget,
)
from stationapp.domain.mac import MacAddress, MacPurpose
from stationapp.drivers.mpcli.driver import PHASE_FLASHING, PHASE_WRITING_MAC
from stationapp.drivers.mpcli.parsers import compare_readback
from stationapp.drivers.mpcli.types import (
    MacReadbackOutcome,
    MacReadbackResult,
    ProcessOutcome,
    ProcessResult,
    ProgrammingOutcome,
    ProgrammingResult,
)
from stationapp.services.golden_rig_service import GoldenRigTestReport
from stationapp.services.programming import (
    ProgrammingStage,
    ProgrammingTarget,
    SlotProgrammingResult,
    SlotReadbackResult,
)

SIMULATED_RIG_ID = "SIMULATED-GOLDEN-RIG"

FAILURE_STAGES = frozenset({
    "stock_program",
    "stock_readback",
    "rf",
    "pricol_program",
    "pricol_readback",
    "app",
    "dfu",
})

_PREFIX = {
    MacPurpose.STOCK_RF_TEST: 0x025A_0000_0000,
    MacPurpose.PRICOL_PRODUCTION: 0x025B_0000_0000,
}


def parse_failures(spec: str) -> frozenset[tuple[str, int]]:
    """Parse 'stage:slot,stage:slot' into a failure set."""
    failures: set[tuple[str, int]] = set()
    for item in (part.strip() for part in spec.split(",")):
        if not item:
            continue
        stage, _, slot = item.partition(":")
        stage = stage.strip().lower()
        if stage not in FAILURE_STAGES or not slot.strip().isdigit():
            raise ValueError(
                f"Invalid simulation failure {item!r}; expected stage:slot with "
                f"stage in {sorted(FAILURE_STAGES)}"
            )
        failures.add((stage, int(slot)))
    return frozenset(failures)


class _Simulator:
    def __init__(self, *, delay_seconds: float, failures: frozenset[tuple[str, int]]) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        self._delay = delay_seconds
        self._failures = failures

    def _fails(self, stage: str, slot_number: int) -> bool:
        return (stage, slot_number) in self._failures

    def _wait(self, token: CancellationToken | None, factor: float = 1.0) -> None:
        deadline = time.monotonic() + self._delay * factor
        while time.monotonic() < deadline:
            if token is not None:
                token.raise_if_cancelled()
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _process(duration: float, *, return_code: int = 0, stdout: str = "") -> ProcessResult:
    return ProcessResult(
        command=("SIMULATED",),
        outcome=ProcessOutcome.COMPLETED,
        return_code=return_code,
        stdout=stdout,
        stderr="",
        duration_seconds=duration,
    )


class SimulatedProgrammer(_Simulator):
    """Stands in for ProgrammingService (MPCLI)."""

    def program_stock(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None, on_phase: Callable[[str], None] | None = None) -> SlotProgrammingResult:
        return self._program(target, ProgrammingStage.STOCK, "stock_program", cancellation_token, on_phase)

    def program_pricol(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None, on_phase: Callable[[str], None] | None = None) -> SlotProgrammingResult:
        return self._program(target, ProgrammingStage.PRICOL, "pricol_program", cancellation_token, on_phase)

    def verify_stock_mac(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None) -> SlotReadbackResult:
        return self._verify(target, "stock_readback", cancellation_token)

    def verify_pricol_mac(self, target: ProgrammingTarget, *, cancellation_token: CancellationToken | None = None) -> SlotReadbackResult:
        return self._verify(target, "pricol_readback", cancellation_token)

    def _program(self, target: ProgrammingTarget, stage: ProgrammingStage, failure_key: str, token: CancellationToken | None, on_phase: Callable[[str], None] | None) -> SlotProgrammingResult:
        if on_phase is not None:
            on_phase(PHASE_FLASHING)
        self._wait(token, 1.0)
        if on_phase is not None:
            on_phase(PHASE_WRITING_MAC)
        self._wait(token, 0.4)

        failed = self._fails(failure_key, target.slot_number)
        result = ProgrammingResult(
            outcome=ProgrammingOutcome.FAILED if failed else ProgrammingOutcome.SUCCESS,
            process=_process(self._delay * 1.4, return_code=1 if failed else 0),
            message="Simulated programming failure" if failed else "Simulated programming complete",
        )
        return SlotProgrammingResult(target=target, stage=stage, programming=result)

    def _verify(self, target: ProgrammingTarget, failure_key: str, token: CancellationToken | None) -> SlotReadbackResult:
        self._wait(token, 0.3)

        # An injected readback failure reports a different MAC: the
        # identity-mismatch path, the most important one to rehearse.
        mac = target.mac
        if self._fails(failure_key, target.slot_number):
            mac = MacAddress((target.mac.value + 1) & 0xFFFFFFFFFFFF)

        readback = MacReadbackResult(
            outcome=MacReadbackOutcome.READ,
            process=_process(self._delay * 0.3, stdout=f"EUID: {mac.colon}"),
            mac=mac,
            message="Simulated readback",
        )
        return SlotReadbackResult(
            target=target,
            readback=readback,
            verification=compare_readback(expected=target.mac, readback=readback),
        )


class SimulatedGoldenRig(_Simulator):
    """Stands in for GoldenRigService over the TLS transport."""

    def run_test(self, *, batch_id: str, station_id: str, jig_id: str, targets: tuple[GoldenRigTarget, ...], cancel_event: threading.Event | None = None) -> GoldenRigTestReport:
        token = CancellationToken()
        if cancel_event is not None and cancel_event.is_set():
            token.cancel()
        self._wait(token, 1.5)

        request = GoldenRigRequest(
            request_id=str(uuid4()),
            batch_id=batch_id,
            station_id=station_id,
            jig_id=jig_id,
            targets=targets,
        )

        results = []
        for target in targets:
            if self._fails("rf", target.slot_number):
                results.append(GoldenRigSlotResult(
                    slot_number=target.slot_number,
                    expected_mac=target.expected_mac,
                    reported_mac=None,
                    connected=False,
                    disconnected=False,
                    error_code="CONNECT_FAILED",
                    detail="Simulated Bluetooth connection failure",
                ))
            else:
                results.append(GoldenRigSlotResult(
                    slot_number=target.slot_number,
                    expected_mac=target.expected_mac,
                    reported_mac=target.expected_mac,
                    connected=True,
                    disconnected=True,
                ))

        response = GoldenRigResponse(
            request_id=request.request_id,
            rig_id=SIMULATED_RIG_ID,
            results=tuple(results),
        )
        response.validate_against(request)
        return GoldenRigTestReport(request=request, response=response)


class SimulatedFunctionalTester(_Simulator):
    """Stands in for the Pricol AT-command functional test."""

    def run_app_test(self, *, slot_number: int, com_port: str, cancellation_token: CancellationToken | None = None) -> bool | None:
        self._wait(cancellation_token, 0.8)
        return not self._fails("app", slot_number)

    def run_dfu_test(self, *, slot_number: int, com_port: str, cancellation_token: CancellationToken | None = None) -> bool | None:
        self._wait(cancellation_token, 0.8)
        return not self._fails("dfu", slot_number)


class SimulatedMacAllocationGateway:
    """Stands in for the LAN server's MAC allocation endpoint."""

    def request_allocation(self, *, station_id: str, jig_id: str, purpose: MacPurpose, minimum_count: int) -> AllocationDocument:
        # A random 24-bit block start inside the simulation prefix keeps
        # successive simulated allocations from colliding in practice.
        start = _PREFIX[purpose] + secrets.randbits(24)
        now = datetime.now(timezone.utc)
        return AllocationDocument(
            allocation_id=f"SIM-{purpose.value}-{uuid4()}",
            station_id=station_id,
            jig_id=jig_id,
            purpose=purpose,
            issued_at=now,
            expires_at=now + timedelta(days=30),
            addresses=tuple(MacAddress(start + offset) for offset in range(minimum_count)),
        )


class InMemoryLanServer:
    """LAN server implementing docs/lan_server_api.md in memory.

    Used by simulation mode and tests through httpx.MockTransport, so the
    real LanServerClient code path is exercised end to end. Set
    ``available = False`` to simulate an outage.
    """

    def __init__(self, *, api_key: str | None = None) -> None:
        import threading as _threading

        self.api_key = api_key
        self.available = True
        self.claims: dict[str, dict] = {}
        self.claim_order: list[str] = []
        self.batches: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.audits: dict[str, dict] = {}
        self.macs: dict[str, dict] = {}
        self.packages: list[dict] = []
        self._lock = _threading.Lock()

    def transport(self):
        import httpx

        return httpx.MockTransport(self.handle)

    def register_foreign_qr(self, module_qr: str, *, station_id: str, batch_id: str = "OTHER-BATCH", slot_number: int = 1) -> None:
        """A QR bound by another station on the line."""
        with self._lock:
            self._claim(module_qr, {"station_id": station_id, "batch_id": batch_id, "slot_number": slot_number, "bound_at": datetime.now(timezone.utc).isoformat()})

    def handle(self, request):
        import json

        import httpx

        if not self.available:
            raise httpx.ConnectError("simulated LAN server outage", request=request)
        if self.api_key and request.headers.get("Authorization") != f"Bearer {self.api_key}":
            return httpx.Response(401, json={"error": "unauthorized"})

        path = request.url.path
        with self._lock:
            if request.method == "POST" and path == "/api/v1/module-qr/claims":
                body = json.loads(request.content)
                owner = {key: body.get(key) for key in ("station_id", "batch_id", "slot_number")}
                owner["bound_at"] = datetime.now(timezone.utc).isoformat()
                existing = self.claims.get(body["module_qr"])
                if existing is not None and not self._same_owner(existing, owner):
                    return httpx.Response(409, json={"status": "DUPLICATE", "owner": existing})
                if existing is None:
                    self._claim(body["module_qr"], owner)
                return httpx.Response(201, json={"status": "CLAIMED"})

            if request.method == "GET" and path == "/api/v1/module-qr":
                since = int(request.url.params.get("since") or 0)
                limit = int(request.url.params.get("limit") or 5000)
                selected = self.claim_order[since:since + limit]
                codes = [{"module_qr": qr, **self.claims[qr]} for qr in selected]
                return httpx.Response(200, json={"codes": codes, "next_cursor": str(since + len(selected))})

            if request.method == "POST" and path.startswith("/api/v1/stations/") and path.endswith("/sync"):
                package = json.loads(request.content)
                self.packages.append(package)
                for batch in package.get("batches", []):
                    self.batches[batch["batch_id"]] = batch
                for event in package.get("events", []):
                    self.events[event["event_id"]] = event
                for audit in package.get("audits", []):
                    self.audits[audit["audit_id"]] = audit
                for mac in package.get("macs", []):
                    self.macs[mac["address"]] = mac
                conflicts = []
                for item in package.get("module_qr", []):
                    owner = {"station_id": package["station_id"], "batch_id": item.get("batch_id"), "slot_number": item.get("slot_number"), "bound_at": item.get("bound_at")}
                    existing = self.claims.get(item["module_qr"])
                    if existing is None:
                        self._claim(item["module_qr"], owner)
                    elif not self._same_owner(existing, owner):
                        conflicts.append({"module_qr": item["module_qr"], "owner": existing})
                return httpx.Response(200, json={"status": "ACCEPTED", "qr_conflicts": conflicts})

        return httpx.Response(404, json={"error": f"no route {request.method} {path}"})

    def _claim(self, module_qr: str, owner: dict) -> None:
        self.claims[module_qr] = owner
        self.claim_order.append(module_qr)

    @staticmethod
    def _same_owner(a: dict, b: dict) -> bool:
        return (a.get("station_id"), a.get("batch_id"), a.get("slot_number")) == (b.get("station_id"), b.get("batch_id"), b.get("slot_number"))
