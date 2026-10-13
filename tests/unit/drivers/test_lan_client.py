"""LAN server client: status-code handling per docs/lan_server_api.md."""

from __future__ import annotations

import httpx
import pytest

from stationapp.drivers.lan.client import LanServerClient, LanServerRejected, LanServerUnavailable


def client_for(handler) -> LanServerClient:
    return LanServerClient(base_url="http://lan", api_key="k", station_id="STN-01", transport=httpx.MockTransport(handler))


@pytest.mark.unit
def test_requests_carry_station_identity_and_key():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(201, json={"status": "CLAIMED"})

    assert client_for(handler).claim_module_qr("QR-1", jig_id="J", batch_id="B", slot_number=1).claimed
    assert seen["authorization"] == "Bearer k"
    assert seen["x-station-id"] == "STN-01"


@pytest.mark.unit
def test_claim_conflict_returns_owner():
    def handler(request):
        return httpx.Response(409, json={"status": "DUPLICATE", "owner": {"station_id": "STN-02", "batch_id": "B9", "slot_number": 4}})

    result = client_for(handler).claim_module_qr("QR-1", jig_id="J", batch_id="B", slot_number=1)
    assert not result.claimed
    assert result.owner.describe() == "station STN-02, batch B9, slot 4"


@pytest.mark.unit
@pytest.mark.parametrize("status", [500, 503])
def test_server_errors_are_retryable(status):
    with pytest.raises(LanServerUnavailable):
        client_for(lambda request: httpx.Response(status)).upload_sync_package({})


@pytest.mark.unit
def test_network_failure_is_retryable():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(LanServerUnavailable):
        client_for(handler).fetch_module_qr(since=None)


@pytest.mark.unit
@pytest.mark.parametrize("status", [400, 401, 422])
def test_client_errors_are_rejections(status):
    with pytest.raises(LanServerRejected):
        client_for(lambda request: httpx.Response(status, json={})).upload_sync_package({})


@pytest.mark.unit
def test_sync_must_be_explicitly_accepted():
    with pytest.raises(LanServerRejected):
        client_for(lambda request: httpx.Response(200, json={"status": "QUEUED"})).upload_sync_package({})
