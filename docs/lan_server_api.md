# LAN Server API — Station Contract (v1)

This is the contract the Station Application uses to talk to the LAN Server: rejecting duplicate module QR codes across the line, and uploading production records. The station side is in `src/stationapp/drivers/lan/client.py`. A reference implementation the station is tested against is `InMemoryLanServer` in `src/stationapp/services/simulation.py`.

## Conventions

- **Base URL:** the `LAN_SERVER_URL` setting, for example `http://lan-server:8080`. Every path below is relative to it.
- **Authentication:** every request carries `Authorization: Bearer <LAN_SERVER_API_KEY>` and `X-Station-Id: <STATION_ID>`. Reject a bad key with `401`.
- **Format:** request and response bodies are JSON, and timestamps are ISO-8601 with a UTC offset.
- **How the station reacts to the status code:**
  - `2xx`: success.
  - `409`: has a meaning only on the claim endpoint (the QR is already owned).
  - Other `4xx`: the request is invalid. The station logs it and does not retry the same data.
  - `5xx` or a network error: the server is unavailable. The station keeps working offline and retries later.

## 0. Login verification

Operators and Supervisors are enrolled **on the LAN Server only**. Stations store no passwords. Every login on any jig is verified by the server, so an enrolled user can log in on any station. The server also owns lockout (repeated wrong passwords) and disabling accounts.

`POST /api/v1/auth/login`

```json
{"user_id": "OPERATOR1", "password": "...", "station_id": "STN-01", "jig_id": "JIG-01"}
```

`POST /api/v1/auth/badge`

```json
{"badge_id": "OPBADGE001", "station_id": "STN-01", "jig_id": "JIG-01"}
```

| Result | Response |
|---|---|
| Accepted | `200 {"user_id": "OPERATOR1", "display_name": "Ravi Kumar", "role": "OPERATOR"}` (`role` is `OPERATOR` or `SUPERVISOR`) |
| Wrong user ID, password or badge | `401 {"error": "INVALID_CREDENTIALS", "message": "Invalid user ID or password"}` |
| Account disabled | `403 {"error": "ACCOUNT_DISABLED", "message": "..."}` |
| Account locked | `423 {"error": "ACCOUNT_LOCKED", "message": "Account locked until 10:42"}` |

- The station shows `message` to the user as-is, so it must be written for operators.
- The station refuses badge-only logins for Supervisors even if the server accepts them.
- If the server is unreachable, nobody can log in. A user who is already logged in stays logged in.
- Until this API exists, stations can run with `AUTH_MODE=test`. That uses built-in test users answering these same endpoints (`src/stationapp/services/test_users.py`) and must never be enabled on a production station.

## 1. Claim a module QR

`POST /api/v1/module-qr/claims`

The station calls this when an operator scans a module QR, before binding the QR to the slot. The station waits at most `QR_CLAIM_TIMEOUT_SECONDS` (2 s by default) for an answer.

```json
{"module_qr": "QR-0001", "station_id": "STN-01", "jig_id": "JIG-01", "batch_id": "<uuid>", "slot_number": 3}
```

| Result | Response |
|---|---|
| Granted, or re-claimed by the same station, batch and slot | `201 {"status": "CLAIMED"}` |
| Already owned by a different station, batch or slot | `409 {"status": "DUPLICATE", "owner": {"station_id": "STN-02", "batch_id": "...", "slot_number": 5, "bound_at": "..."}}` |

**Requirement:** the server must grant each QR to exactly one owner, atomically. This is what stops two stations from binding the same code.

## 2. List known module QR codes

`GET /api/v1/module-qr?since=<cursor>&limit=<n>`

The station calls this at the start of every sync. It caches the result, so a duplicate is still rejected while the server is unreachable.

```json
{"codes": [{"module_qr": "QR-0001", "station_id": "STN-02", "batch_id": "...", "slot_number": 5, "bound_at": "..."}],
 "next_cursor": "12345"}
```

- `since` is omitted on the first call.
- The station saves `next_cursor` and sends it back next time.
- The station stops paging when it receives an empty `codes` list or an unchanged cursor.

## 3. Upload a sync package

`POST /api/v1/stations/{station_id}/sync`

A sync runs:
- automatically when `SYNC_MODULE_THRESHOLD` completed modules (1000 by default) are waiting;
- when someone presses **Sync now**;
- before a Supervisor closes the session;
- on retry after a failure.

One sync may send several packages, and each must be acknowledged before the station sends the next.

```json
{
  "schema_version": 1,
  "client_transaction_id": "<uuid>",
  "station_id": "STN-01",
  "jig_id": "JIG-01",
  "sent_at": "...",
  "batches":   [ {"batch_id": "...", "programming_mode": "...", "operator_id": "...", "session_id": "...",
                  "created_at": "...", "committed_at": "...",
                  "slots": [{"slot_number": 1, "disposition": "PASS|FAIL|HOLD|EMPTY", "module_qr": "...",
                             "stock_mac": "...", "pricol_mac": "...", "stock_rf_outcome": "...",
                             "pricol_app_passed": true, "pricol_dfu_passed": true, "hold_reason": null, ...}]} ],
  "macs":      [ {"address": "025A00A88EA3", "purpose": "...", "status": "CONFIRMED|HOLD|...", "batch_id": "...",
                  "slot_number": 1, "module_qr": "...", "reserved_at": "...", "confirmed_at": "...", ...} ],
  "module_qr": [ {"module_qr": "...", "batch_id": "...", "slot_number": 1, "bound_at": "...", "server_status": "UNCONFIRMED|CLAIMED|..."} ],
  "events":    [ {"event_id": "...", "batch_id": "...", "slot_number": 1, "event_type": "STOCK_PROGRAMMED",
                  "payload": {...}, "occurred_at": "..."} ],
  "audits":    [ {"audit_id": "...", "user_id": "...", "record_type": "...", "action": "...", "reason": "...",
                  "previous_value": {...}, "new_value": {...}, "created_at": "..."} ]
}
```

The server responds with:

```json
{"status": "ACCEPTED",
 "qr_conflicts": [{"module_qr": "QR-0002", "owner": {"station_id": "STN-09", "batch_id": "...", "slot_number": 1, "bound_at": "..."}}]}
```

### Server requirements

- **Idempotency.** Store every record by its natural unique key: `batch_id`, `event_id`, `audit_id`, MAC `address`, `module_qr`. If an acknowledgement is lost, the station re-sends the same records in a new package, so a repeat must be a no-op. Original timestamps and `station_id` must be preserved.
- **Conflict reporting.** For each `module_qr` entry whose code the server already holds for a different owner, keep the existing owner and report the code in `qr_conflicts`. This covers codes the station bound while offline, which the station marks `UNCONFIRMED`. The station marks a reported code `CONFLICT`, records a `QR_CONFLICT_REPORTED` event, and shows it to the Supervisor.
- **Accepted means stored.** Return `ACCEPTED` only after the whole package is durably stored. The station then marks those batches `UPLOADED` and advances its event and audit cursors.

## Not yet covered

MAC allocation requests (README steps 5–6) are not part of this contract yet. The station currently fills its cache by importing allocation documents.
