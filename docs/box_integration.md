# UnisemBLE Box Integration

How StationApp talks to the UnisemBLE box (the site's LAN server) through the vendor SDK `unisemble_client` 7.0.0 (for a 7.0 box; a 7.0 station does not work with an older box, nor an older station with a 7.0 box). This replaces the earlier StationApp-defined LAN contract (`docs/lan_server_api.md`), which no server implemented.

Reference material from the vendor, kept with the wheel in `vendor/unisemble/`:

- `DEVELOPER-GUIDE.md`: what each SDK call does and raises. Section numbers below ("guide 4.5") refer to it.
- `STATION-APP-REQUIREMENTS.md`: rules R1–R17 that a production station keeps. The table at the end maps each one to StationApp code.

## What the box does, and what it does not

The box owns the site's MAC addresses. It gives this station **blocks** of them, checks logins (phone number and 6-digit PIN), and records **once per finished tray** which module (QR) got which address and whether it passed. It does not know the station's process steps (Stock programming, RF test, Pricol, functional tests). Those records stay in the local database.

| Moment | SDK call | StationApp code |
|---|---|---|
| Someone logs in / logs out | `login`, `change_pin`, `logout` | `services/box_auth.py` |
| A block is missing or running low | `request_block` | `services/box_blocks.py` |
| The station switched to its reserve block | `promote_block` | `services/box_blocks.py` |
| A tray is committed | `report` | `services/box_reporting.py` |
| A supervisor clears a hold at the station | `clear_hold` | `services/box_reporting.py`, `ClearHoldDialog` |

## Layout

```
vendor/unisemble/                     the SDK wheel (pinned 7.0.0) and the vendor's docs
src/stationapp/drivers/box/
    gateway.py                        BoxGateway: the only module holding the SDK client
    demo.py                           in-memory demo box (SIMULATION_MODE, AUTH_MODE=test, tests)
src/stationapp/data/
    box_block_repository.py           box_blocks table; reservation from the current block
    sync_repository.py                report queue (outbox), report history, QR registry
src/stationapp/services/
    box_auth.py                       phone + PIN login, roles, forced PIN change
    box_blocks.py                     current / reserve blocks, promotes
    box_reporting.py                  reports, hold state, background scheduler
    batch_codes.py                    the session's running Batch Code (batch_id)
src/stationapp/domain/batch_code.py   Batch Code rules: entry, +1, exact split
src/stationapp/data/batch_code_repository.py   station_settings.batch_code
alembic/versions/a6c3e9f2d815_box_blocks.py, c4e8a1d7b392_reworked_module_qr.py
tests/integration/services/test_box_integration.py
```

`unisemble-sdk-handover-7.0.0/unisem-station-src` is the vendor's **reference station app** and demo box. It is not the box's server code, and StationApp does not import it.

Install: `pip install --find-links vendor/unisemble -e .` (or `pip install -r requirements.txt`).

## One connection, one person

`BoxGateway` wraps one `UnisembleClient` for the life of the app. A malformed `BOX_URL` makes the SDK raise `ValueError` when the client is created; `box_url_problem()` checks the URL first, so the app starts without a box connection and the READY check names the problem. The SDK runs one call at a time. Interactive calls (login, blocks, clearing a hold) wait their turn for up to 30 s. A background report does not wait: on `ClientBusy` it keeps its modules for the next attempt.

The box keeps **one logged-in person per station**. Any `login()` ends the previous person's box session, and reports are recorded against whoever is logged in. Consequences in StationApp:

- An Operator login takes the box session; that is the normal case.
- A Supervisor login or authorization (elevation, HOLD disposition, configuration change) takes the box session from the Operator. `StationAccessService` therefore logs the Operator out locally, and the Operator logs in again afterwards.
- If the box ends the session itself (idle 2 h, 12 h limit, shift boundary), the next report raises `SessionExpired`. The Operator is logged out locally, the trays stay queued, and they are reported after the next login.
- Before an Operator logs out, the queued trays are reported first (guide 4.9), unless the box was already failing.

Roles come from the box (SDK 6.1 `User.role`): `admin` and `supervisor` become `SUPERVISOR`; `user` becomes `OPERATOR`.

## MAC addresses

| Mode | Stock stages | Final MAC (from a box block, reported) |
|---|---|---|
| Stock Only | box block, master range `BOX_MASTER_RANGE_STOCK` | the Stock MAC |
| Stock + Pricol | temporary per-slot MAC: the built-in `TEMPORARY_STOCK_MACS` (`0A:FF:00:00:00:01` for slot 1 … `:08`), or `TEMP_STOCK_MAC_BASE + slot - 1` when set. Reused on every tray and by every station, not stored in the MAC cache, never reported | the Pricol MAC, master range `BOX_MASTER_RANGE_PRICOL` |

A repeated temporary MAC is harmless: it lives on a module only between the Stock RF test and the Pricol MAC write, and the box checks the reported (final) MACs for duplicates.

### Blocks

`box_blocks` holds each block's state: `CURRENT`, `RESERVE` or `CLOSED`. All of a block's addresses are inserted as `AVAILABLE` rows of `allocated_macs` (allocation `BOX-<block id>`) in the same transaction as the block, so the existing MAC lifecycle (RESERVED → PROGRAMMING → ISSUED → CONFIRMED / HOLD, startup quarantine) applies unchanged.

- **Reservation** (`BatchRepository.reserve_macs`) takes the lowest AVAILABLE addresses of the **current** block only. If the current block cannot fill the tray and a reserve is held, it closes the current block, makes the reserve current, and records the owed promote. All of this happens in the same `BEGIN IMMEDIATE` transaction that reserves the tray, before anything is programmed. A tray never spans two blocks; the old block's remaining addresses are never used.
- **Top-up** (`BoxBlockService.ensure`, called by the READY check on a worker thread and after each report):
  1. Sends every owed promote first.
  2. Requests a current block if there is none.
  3. Requests a reserve when the current block has `MAC_CACHE_THRESHOLD` addresses or fewer left.

  Only purposes in use are topped up.
- **One pair per station.** The box keeps one current + reserve pair per station, whatever the master range. The station therefore holds blocks for one purpose (the mode in use) at a time. The first block saved for the other purpose closes every open block of the previous purpose, in the same transaction (`BoxBlockRepository.add`); their remaining addresses are lost, which the box accepts. If the box is down at the mode change, nothing is closed.
- **Lost state, mode change** (guide 4.11): if the box answers `reserve_already_held` with an open block the station does not hold for the purpose it is requesting (blocks it lost track of, or the previous mode's pair), that pair is promoted (closed) and the request is repeated. A block held for the requested purpose is never closed this way. After a mode change the box may file the new block as a reserve; the station programs from it at once, and the next reserve request promotes it (as in guide 4.11).
- **Another box**: the station remembers which box issued its blocks (`station_settings.box_identity`). On a different box (for example demo → real), every open block is closed and fresh ones are drawn.

### Restarting a tray

A tray keeps the box addresses it was given until it is committed (and so reported). `ProductionController.restart_batch` (the RESTART TRAY button) sends the tray back to `STOCK_MACS_RESERVED` at any step before the commit, QR scanning included (a label that cannot be read):

- The batch keeps its `batch_id`. `Batch.restart` gives every **loaded** slot a fresh slot record, so a slot that was not detected comes back, and rewinds each MAC record to `RESERVED` for the same batch and slot (`AllocatedMac.rewind_for_restart`; whatever was written with it is not reported).
- **Stock MACs:** each slot gets its own again. In Stock + Pricol mode these are the temporary MACs. In Stock only, a slot whose MAC was released as not detected first gets a fresh reservation; if that fails, the tray is left unchanged.
- **Pricol MACs** wait in `Batch.carried_pricol_macs` (saved as `RESERVED`). At the Pricol step each eligible slot takes its carried MAC, and only slots without one reserve a new MAC. A carried MAC not needed by the final attempt (the slot failed earlier this time) is released to `AVAILABLE` at the commit.
- **QRs:** `SqlProductionStore.restart` deletes this tray's QR registry rows, except a re-worked module's (its approval stays with the tray). `module_qr_owner(..., ignore_batch=)` does not count the tray's own binding, so rescanning never raises the QR-reuse hold. At the commit, registry rows of the tray whose QR is not in the final slots are removed.
- Event `BATCH_RESTARTED` records who restarted it, from which state, and the MACs kept. The RF evidence and test events of every attempt stay in the history; the report carries only the final attempt.
- A tray in `HOLD` needs a Supervisor; a running step is cancelled first (the panel waits for the worker to finish).

A module taken out of the jig after a restart may still carry an address the tray now gives to another module. The guide tells Operators not to use such a module until it has been through the station again. The box sees only the final report.

A power cut is not a restart: startup recovery still quarantines every address left in `PROGRAMMING`, and the unfinished tray is not resumed.

## Reports and holds

A committed batch is one tray. `ProductionController._finalise` writes the batch record to the outbox in the same transaction as the commit. `BoxReportScheduler` then reports it on a background thread. A report holds every module that received a box address, with its QR, that MAC, whether it passed (`disposition == PASS`) and its Batch Code as `batch_id` (see Batch Codes); a failed or held module's reason goes in `note`. Up to 500 modules go in one report, so queued trays go out together.

| Outcome | What StationApp does |
|---|---|
| Recorded | Outbox row `UPLOADED`, batch `UPLOADED`, QR registry `SYNCED`, event `UPLOAD_CONFIRMED` |
| `ClientBusy`, `BoxUnreachable`, `Refused` | Trays stay queued; retried every `BOX_REPORT_RETRY_SECONDS` |
| `SessionExpired` / no one logged in | Trays stay queued; reported after the next login |
| `rejected` (`duplicate`, `not_this_station`) | QR registry `CONFLICT`, event `QR_CONFLICT_REPORTED`, module listed on the hold screen |
| `on_hold` | Saved in `station_settings.box_hold`; the READY check fails ("Station hold"), so no new tray starts until a Supervisor clears it with their phone and PIN |
| `reprogrammed` | Recorded like `accepted`: the box retires the module's previous MAC |

Re-sending a report the box may already have recorded is safe: the box answers `accepted`. The box does not look for gaps in the addresses reported: an address left unreported (a batch aborted before its QR scan, a module quarantined at startup) causes no hold.

### A QR scanned again (same QR, new MAC)

A QR already bound on this station, scanned in a later tray, means a re-worked module or a wrong label. `ProductionController.scan_qr` does not bind it: it puts the station on hold (`BoxReportService.hold_for_reused_qr`, saved in `station_settings.qr_reuse_hold`, audit `QR_REUSE_HOLD`) and every further scan is refused. The READY check fails, the session bar shows LINE STOPPED, and `ClearHoldDialog` lists the QR and where it was bound.

A Supervisor or Admin clears it with their phone and PIN. The box verifies them through `clear_hold` (which also clears any box hold); this needs the box to be reachable. Clearing approves each held QR **once**: scanned again, it is bound to the new slot (`QrRegistryRepository.rebind`, event `MODULE_QR_BOUND` with `rework_of`), and the report carries the new MAC, which the box records as `reprogrammed`. `batch_slots.module_qr` is therefore no longer unique (migration `c4e8a1d7b392`); the QR registry still holds one row per QR, pointing at the latest binding. A QR scanned twice within one tray is still refused without a hold.

## Batch Codes (SDK 7.0 `batch_id`)

SDK 7.0 requires a batch ID on every reported module (1 to 64 printable ASCII characters). StationApp calls it the **Batch Code**, because a StationApp "batch" is one tray. Rules agreed with the user (2026-11-05):

- **Threshold.** The Supervisor or Admin enters the Batch Code threshold (passed modules per code) when starting the production session (Station configuration → Session). `StationAccessService.start_session(..., batch_code_threshold=)` saves it in `station_settings.batch_code` in the session's transaction; the audit `SESSION_STARTED` records it.
- **Starting series.** The Operator enters it once per session, before the first tray (Production screen, ENTER BATCH CODE). Later Operators carry on with the running code. Once a series runs, only a Supervisor may start a new one, with a reason (audit `BATCH_CODE_SERIES_STARTED` / `BATCH_CODE_SERIES_CHANGED`). Closing the session ends the series; a new session needs a new threshold and series. No tray starts without a series (`ProductionController.start_batch`).
- **Increment.** The code must end in a number; the next code is that number plus one, keeping its zero padding: `S5A09` → `S5A10`, `S5A99` → `S5A100`.
- **Exact split.** Codes are given when a tray is committed (`ProductionController._allocate_batch_codes`), slots in ascending order. Each code holds exactly `threshold` passed modules, so a tray may straddle two codes. A failed module gets the code current at its slot and is not counted. The code moves on with the first pass beyond the threshold.
- **Only complete cycles count.** A tray is counted only at its commit; aborted trays are never committed, and the attempts before a restart are not counted separately. The new count is saved in the commit's transaction (`SqlProductionStore.save_and_enqueue(batch_code_state=)`), so it always matches the trays queued for the box.
- **Records.** Each slot of the outbox record has `batch_code` (`schema_version` 2); event `BATCH_COMMITTED` lists the codes and the state after the tray.
- **Upgrade.** A tray queued before this change has no Batch Code: the SDK refuses it locally (`InvalidBatchIdError`) and the report stays queued with that message. Report every queued tray before upgrading the box and the stations (guide section 9).

## Configuration

| Setting | Meaning |
|---|---|
| `BOX_URL` | "Workstation app (SDK base_url)" from the box installer, e.g. `http://box-host:3001` |
| `BOX_STATION_KEY` | Issued once on the box console (Stations) |
| `BOX_MASTER_RANGE_STOCK`, `BOX_MASTER_RANGE_PRICOL` | Master range names from the box console (Pools) |
| `BOX_BLOCK_SIZE` | Addresses per block request, 100–20000 (default 5000) |
| `MAC_CACHE_THRESHOLD` | Low water for fetching the reserve block |
| `TEMP_STOCK_MAC_BASE` | Optional, Stock + Pricol: empty uses the built-in list; otherwise a unicast address, and every slot of an 8-up jig must fit |
| `BOX_REPORT_RETRY_SECONDS` | Retry interval for queued reports |
| `AUTH_MODE=test` | Uses the in-memory demo box for everything; testing only |

`SIMULATION_MODE` without `BOX_URL` also uses the demo box. Use a separate `DATABASE_URL` for simulation: trays committed in simulation would otherwise be reported to the real box and rejected.

## Requirements map (STATION-APP-REQUIREMENTS.md)

| Rule | Where it is kept |
|---|---|
| R1, R2 saved state, atomic write | SQLite transactions: blocks, reservation, block switch and owed promote (`reserve_macs`); trays waiting to report (outbox, written with the commit); hold (`station_settings`). StationApp keeps these in SQLite, not one JSON document. **Raise this at the design review.** |
| R3 power-cut test | Not yet run on hardware |
| R4 position saved before programming | Addresses are RESERVED and committed before MPCLI starts; PROGRAMMING is persisted before the write |
| R5, R6 own blocks only; no tray spans blocks | `current_block_addresses` |
| R7 right master range | `BOX_MASTER_RANGE_*` per purpose |
| R8–R10 reserve, switch, owed promote | `box_block_repository`, `BoxBlockService` |
| R11, R12 report every programmed module with its batch ID; keep unsent ones | `box_reporting.modules_of`, outbox; Batch Code given at commit |
| R13 line does not wait | Background `BoxReportScheduler`; blocks topped up on the READY worker |
| R14–R16 holds | `HoldState`, "Station hold" READY check, `ClearHoldDialog` |
| R17 redraw, never resume | Guide 4.11 recovery; box-identity check. A database copied from another PC is not detected yet. |

## Decisions confirmed with the box vendor (2026-11-03)

1. **One block pair per station**, whatever the master range, as the demo box does. Stock + Pricol programs temporary Stock MACs, so a tray consumes box addresses from one range only; a mode change gives up the old pair (see Blocks).
2. **No hold for gaps.** The box does not check for unreported addresses inside a block.
3. **Re-worked modules** (same QR, new MAC) hold the station until a Supervisor or Admin clears it; the module is then reported and the box records it as re-programmed.
4. **Temporary Stock MACs** come from a fixed list on the station; repeats across trays and stations are acceptable.
5. **Restart** (2026-11-03): a tray may be restarted at any step, QR scanning included; its MACs are reused until it is reported (see Restarting a tray).
6. **Batch Codes** (SDK 7.0, 2026-11-05): every reported module carries its Batch Code (see Batch Codes).

Still to verify on the real box: a `promote_block` from a block of one master range to a block of another (used after a mode change), and R3, the power-cut test.
