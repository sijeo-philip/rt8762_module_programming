"""In-memory demo box behind the SDK's FakeTransport - TESTING ONLY.

Adapted from unisem-station-src/reference/demo_server.py (SDK 6.1.1).
It answers the station calls closely enough to run the station without
a box: login and PIN change, blocks and promote, reports (duplicates,
re-programming, holds) and a supervisor clearing a hold. Like the box,
it keeps one current and one reserve block per station, whatever the
master range; unlike the reference, it serves two master ranges.

Never used on a production station: SIMULATION_MODE or AUTH_MODE=test.
"""

from __future__ import annotations

import uuid

from unisemble_client import int_to_mac, mac_to_int
from unisemble_client.errors import TransportError
from unisemble_client.transport import FakeTransport

from stationapp.drivers.box.gateway import BoxGateway

DEMO_URL = "http://demo-box.local"
DEMO_KEY = "demo-station-key"
DEMO_STOCK_RANGE = "DEMO-STOCK"
DEMO_PRICOL_RANGE = "DEMO-PRICOL"

# phone: (user id, PIN, role, name, must change PIN)
DEMO_USERS = {
    "9000000001": ("OP-1", "123456", "user", "Demo Operator", False),
    "9000000002": ("OP-2", "000000", "user", "New Operator", True),
    "9000000003": ("SUP-1", "246810", "supervisor", "Demo Supervisor", False),
    "9000000004": ("ADM-1", "135790", "admin", "Demo Admin", False),
}

_ERR = "https://demo-box.local/errors/"
_RANGE_SIZE = 1_000_000


def _problem(status: int, slug: str, detail: str, **extra):
    return status, {"type": _ERR + slug, "title": slug, "status": status, "detail": detail, **extra}


class DemoBox:
    def __init__(self, master_ranges: tuple[str, ...] = (DEMO_STOCK_RANGE, DEMO_PRICOL_RANGE)) -> None:
        self.people = {phone: {"id": i, "pin": p, "role": r, "name": n, "must_change": c} for phone, (i, p, r, n, c) in DEMO_USERS.items()}
        # Separate address spaces per range, in the locally administered space.
        self.next_start = {name: mac_to_int("0A0000000000") + index * _RANGE_SIZE for index, name in enumerate(master_ranges)}
        self.range_end = {name: start + _RANGE_SIZE - 1 for name, start in self.next_start.items()}
        self.session: str | None = None
        self.blocks: list[dict] = []        # {id, range, start, end, state}
        self.modules: dict[int, dict] = {}  # mac -> {qr, passed, state}
        self.holds: list[dict] = []         # {mac, reason}
        self.reports: dict[str, tuple[str, dict]] = {}
        self.cleared_by: list[str] = []
        self.down = False                   # True: every call gets no answer
        self.calls: list[str] = []

    # -- people ----------------------------------------------------------

    def _login(self, h, b, q):
        p = self.people.get(b.get("phone"))
        if p is None or p["pin"] != b.get("pin"):
            return _problem(401, "invalid-credentials", "Login failed")
        if p["must_change"]:
            return _problem(409, "must-change-pin", "This PIN must be changed before logging in.")
        self.session = uuid.uuid4().hex
        return 201, {"operator_session_token": self.session,
                     "operator": {"identity_user_id": p["id"], "phone": b["phone"], "display_name": p["name"], "role": p["role"]}}

    def _change_pin(self, h, b, q):
        p = self.people.get(b.get("phone"))
        if p is None or p["pin"] != b.get("current_pin"):
            return _problem(401, "invalid-credentials", "Login failed")
        new = str(b.get("new_pin", ""))
        if len(new) != 6 or not new.isdigit() or new == p["pin"]:
            return _problem(400, "invalid-request", "new_pin must be 6 digits and differ from the current PIN")
        p["pin"], p["must_change"] = new, False
        return 200, {"ok": True}

    def _logout(self, h, b, q):
        if h.get("X-Operator-Session") == self.session:
            self.session = None
        return 200, {"clock_out_at": None, "session_duration_s": 0}

    def end_session(self) -> None:
        """Demo control: the box ends the session (idle, shift end)."""
        self.session = None

    # -- blocks ----------------------------------------------------------

    def _block(self, state: str) -> dict | None:
        return next((x for x in self.blocks if x["state"] == state), None)

    def _request_block(self, h, b, q):
        name = b.get("master_range")
        if name not in self.next_start:
            return _problem(404, "not-found", f"No master range named {name!r}")
        size = int(b.get("requested_size", 0))
        if size <= 0:
            return _problem(422, "unprocessable", "requested_size must be > 0")
        opened, reserved = self._block("open"), self._block("reserved")
        if opened and reserved:
            return _problem(409, "reserve-already-held", "This station already holds a current and a reserve block.",
                            open_block_id=opened["id"], reserved_block_id=reserved["id"])
        start = self.next_start[name]
        if start + size - 1 > self.range_end[name]:
            return _problem(409, "pool-exhausted", "Every address in this master range has been handed out.")
        block = {"id": len(self.blocks) + 1, "range": name, "start": start, "end": start + size - 1,
                 "state": "reserved" if opened else "open"}
        self.next_start[name] += size
        self.blocks.append(block)
        return 201, {"block_id": block["id"], "state": block["state"], "range_start": int_to_mac(block["start"]),
                     "range_end": int_to_mac(block["end"]), "size": size}

    def _promote(self, h, b, q):
        old = next((x for x in self.blocks if x["id"] == b.get("old_block_id")), None)
        new = next((x for x in self.blocks if x["id"] == b.get("new_block_id")), None)
        if old is None or new is None:
            return _problem(404, "not-found", "Block not found for this station")
        old["state"], new["state"] = "closed", "open"
        return 200, {"outcome": "SATISFIED"}

    # -- reports and holds -----------------------------------------------

    def _owner(self, mac: int) -> dict | None:
        return next((x for x in self.blocks if x["start"] <= mac <= x["end"]), None)

    def _hold(self, mac: int | None, reason: str) -> None:
        if mac is None or not any(h["mac"] == mac for h in self.holds):
            self.holds.append({"mac": mac, "reason": reason})

    def hold_station(self, reason: str) -> None:
        """Demo control: a supervisor puts the station on hold from the console."""
        self._hold(None, reason)

    def _report(self, h, b, q):
        key = repr(b["modules"])
        stored = self.reports.get(b["report_id"])
        if stored is not None:
            return (200, stored[1]) if stored[0] == key else _problem(409, "idempotency-conflict", "report_id reused")
        if self.session is None or h.get("X-Operator-Session") != self.session:
            return _problem(401, "operator-session-expired", "No operator is logged in, or the session has ended.")
        results = [{"qr": m["qr"], "mac": m["mac"], **self._record(m)} for m in b["modules"]]
        answer = {"report_id": b["report_id"], "results": results, "on_hold": bool(self.holds),
                  "hold_reason": self.holds[0]["reason"] if self.holds else None}
        self.reports[b["report_id"]] = (key, answer)
        return 200, answer

    def _record(self, m: dict) -> dict:
        mac, qr = mac_to_int(m["mac"]), m["qr"]
        if self._owner(mac) is None:
            self._hold(mac, f"Address {m['mac']} was never given to this station.")
            return {"outcome": "rejected", "reason": "not_this_station"}
        have = self.modules.get(mac)
        if have is not None and have["qr"] != qr:
            self._hold(mac, f"Address {m['mac']} is already on module {have['qr']}.")
            return {"outcome": "rejected", "reason": "duplicate"}
        state = "confirmed" if m["passed"] else "quarantined"
        if have is not None:
            have.update(passed=m["passed"], state=state)
            return {"outcome": "accepted"}
        old = next((x for x in self.modules.values() if x["qr"] == qr and x["state"] != "unused"), None)
        if old is not None:
            old["state"] = "unused"
        self.modules[mac] = {"qr": qr, "passed": m["passed"], "state": state}
        return {"outcome": "reprogrammed" if old is not None else "accepted"}

    def _clear_hold(self, h, b, q):
        p = self.people.get(b.get("phone"))
        if p is None or p["pin"] != b.get("pin"):
            return _problem(401, "invalid-credentials", "Login failed")
        if p["role"] not in ("supervisor", "admin") or p["must_change"]:
            return 200, {"cleared": False}
        self.holds.clear()
        self.cleared_by.append(p["name"])
        return 200, {"cleared": True, "cleared_by": p["name"]}

    # -- wiring ----------------------------------------------------------

    def transport(self) -> FakeTransport:
        box = self

        class _Transport(FakeTransport):
            def request(self, method, url, headers, body, timeout):
                box.calls.append(url.rsplit("/api/v1", 1)[-1])
                if box.down:
                    raise TransportError("demo box switched off")
                return super().request(method, url, headers, body, timeout)

        transport = _Transport()
        for path, handler in {
            "/operator/login": self._login,
            "/operator/change-pin": self._change_pin,
            "/operator/logout": self._logout,
            "/blocks/request": self._request_block,
            "/blocks/promote": self._promote,
            "/reports": self._report,
            "/station/hold/clear": self._clear_hold,
        }.items():
            transport.handle("POST", "/api/v1" + path, handler)
        return transport

    def gateway(self) -> BoxGateway:
        return BoxGateway(box_url=DEMO_URL, station_key=DEMO_KEY, transport=self.transport())
