# Site Test Plan: Box, Golden Rig and Jig

Tests to run once the Box, the Golden Rig and the DUTs (modules) are connected to a station. The automated tests cover the station logic against an in-memory demo Box; these tests check what only the real system can show.

**How to report back.** For every test, send:
- the test number and **PASS / FAIL / BLOCKED**;
- what you saw if it differed from *Expected* (a photo of the screen is fine);
- the station log for the day: `logs\` in the StationApp folder (`LOG_DIR`);
- for Box tests, what the Box console shows for the station (Stations, Blocks, Modules, Holds).

Write down the date, `STATION_ID`, the Box software version and the StationApp git commit at the top of your report.

## 0. Before you start

| # | Check |
|---|---|
| 0.1 | `.env`: `AUTH_MODE=server`, `SIMULATION_MODE=false`, `BOX_URL`, `BOX_STATION_KEY`, `BOX_MASTER_RANGE_STOCK`, `BOX_MASTER_RANGE_PRICOL` set; `TEMP_STOCK_MAC_BASE` empty. Use a **fresh** `DATABASE_URL` file for the first run of this plan. |
| 0.2 | `pip install -r requirements.txt` done; `python scripts/verify_env.py` reports `unisemble_client` 6.1.1. |
| 0.3 | `alembic upgrade head` (or the app's start-up migration) ran without errors. |
| 0.4 | On the Box console: the station exists, both master ranges exist, and test people exist: one **user** (Operator), one **supervisor**, one **admin**, and one user whose PIN must be changed at first login. |
| 0.5 | Golden Rig bound and approved ([guide C4](operator_supervisor_guide.md#c4-bind-the-golden-rig--first-time-supervisor)); jig slots bound ([C3](operator_supervisor_guide.md#c3-bind-jig-slots-to-com-ports-supervisor)). |

## 1. Box connection and login

| # | Steps | Expected |
|---|---|---|
| 1.1 | Start the app. Look at the footer and the login screen. | Footer shows `Box: <BOX_URL>`. No **DEMO BOX** text. |
| 1.2 | Set `BOX_URL=box-host:3001` (no `http://`), restart. Then restore it. | The app starts; **Check station** shows **Box** red: *must look like http://…*. Nobody can log in. |
| 1.3 | Log in as the Operator. | Operator screen (no configuration tabs). |
| 1.4 | Log out; log in as Supervisor, then as Admin. | Full screen with all tabs, for both. |
| 1.5 | Log in as the must-change-PIN user. | *New PIN* fields appear; after setting a new 6-digit PIN the user is logged in. Logging in again with the old PIN fails. |
| 1.6 | Type a wrong PIN repeatedly. | *Phone number or PIN not recognised*, then *Too many wrong PINs. Try again in N minutes*. |
| 1.7 | Pull the station's network cable, try to log in. Reconnect. | Within about 20 s: *the box is not answering*. Logging in works again after reconnecting. |
| 1.8 | Operator logged in; Supervisor presses **Supervisor** and logs in, then logs out. | Warning that the Operator must log in again; afterwards the login screen. The Box console shows the Operator's session ended. |

## 2. MAC blocks

| # | Steps | Expected |
|---|---|---|
| 2.1 | Production settings: **Stock + Pricol**. Operator: **Check station**. | **Pricol MACs**: *5000 Pricol MACs in box blocks (block N: 5000 left; no reserve yet)*. **Temporary Stock MACs**: *built-in list, slot 1 = 0A:FF:00:00:00:01*. Box console: one block for this station in the Pricol range. |
| 2.2 | Restart the app, **Check station** again. | Same block, no new block on the Box console. |
| 2.3 | Set `BOX_BLOCK_SIZE=100` and `MAC_CACHE_THRESHOLD=8` on a test station; run batches until fewer than 8 addresses are left in the block, then **Check station**. | A second block (reserve) appears on the console. When the current block cannot fill a batch, the next batch takes all its MACs from the reserve (never split across blocks), and the console then shows the old block closed (promote sent). |
| 2.4 | **Mode change.** Switch to **Stock only**, **Check station**. | **Stock MACs** now shows a block from the Stock range; no *call support* message. Console: the Pricol pair is closed, the new Stock block is open or reserved. *This is the cross-range promote: the most important Box test.* |
| 2.5 | Run one Stock only batch; then let the Stock block reach low water (as in 2.3) and **Check station**. | A Stock reserve is drawn without errors. Console: never more than two open blocks for this station. |
| 2.6 | Switch back to **Stock + Pricol**, **Check station**, run one batch. | A new Pricol block; the batch's Pricol MACs come from it. |
| 2.7 | With the network cable pulled, switch mode and press **Check station**. Reconnect and check again. | While disconnected: *the box is not answering*, the old mode's blocks are kept. After reconnecting: the new block arrives. |

## 3. Production flow on the real jig

Use modules with fresh QR labels. Note each module's QR and the MACs shown on screen.

| # | Steps | Expected |
|---|---|---|
| 3.1 | **Stock + Pricol**, full jig (4 or 8 modules). Run the batch to **BATCH COMPLETE**. | Stock stages use the temporary MACs: slot N = `0A:FF:00:00:00:0N`; the RF test passes with them. The Pricol MAC written and read back is from the Box block. |
| 3.2 | Read a finished module's MAC with a phone BLE scanner. | The **Pricol MAC**, not the temporary one. |
| 3.3 | Run a second Stock + Pricol batch. | The same temporary MACs again; new Pricol MACs. |
| 3.4 | Two stations in Stock + Pricol at the same time, near the same Golden Rig (if available). | Both pass the RF test even though both use the same temporary MACs. **Report any RF failure here: it decides whether each station needs its own temporary list (`TEMP_STOCK_MAC_BASE`).** |
| 3.5 | **Stock only**, partial jig (e.g. slots 1, 3). | Only those slots run; their Stock MACs are from the Stock block. |
| 3.6 | A batch with one empty slot ticked. | That slot shows **NOT DETECTED - skipped**; its MAC is reused in the next batch. |
| 3.7 | A batch where one module fails (e.g. not reset before the RF test). | The failed module is still scanned and appears on the Box console as not passed. |

## 4. Reports

| # | Steps | Expected |
|---|---|---|
| 4.1 | After 3.1: wait a few seconds; check the top bar and the Box console. | Top bar *Box: 0 modules to report*. Console lists every module of the batch with its QR, final MAC and pass/fail. |
| 4.2 | Pull the network cable; complete two batches; reconnect. | During the outage the line keeps running and the top bar shows modules queued. Within `BOX_REPORT_RETRY_SECONDS` (60 s) of reconnecting, all of them reach the console. |
| 4.3 | End the Operator's session on the Box console (or wait for the idle timeout); complete a batch. | Operator is logged out with *Your session has ended*; after logging in again, the batch is reported. |
| 4.4 | Operator logs out with batches waiting. | The waiting batches are reported before logout (console shows them). |

## 5. Holds

| # | Steps | Expected |
|---|---|---|
| 5.1 | **QR scanned again.** Put a module from an earlier batch (already reported) back in the jig, run a batch, scan its old QR. | *STATION ON HOLD: QR … is already bound at …*; further scans refused; red **LINE STOPPED - Clear hold**; **Check station** fails on **Station hold**. |
| 5.2 | Restart the app during the hold of 5.1. | The hold is still there. |
| 5.3 | Try to clear with the Operator's phone and PIN. | *This person cannot clear holds*. The hold stays. |
| 5.4 | Clear with the **Admin** (or Supervisor) phone and PIN; scan the same QR again; finish the batch. | The QR is accepted. Console: the module now has the new MAC, the old MAC is marked unused / re-programmed. The Operator stayed logged in. |
| 5.5 | Box hold: put the station on hold from the Box console (or report a module whose MAC is already on another module), then complete a batch. | After the report: red **LINE STOPPED**; the dialog title is *Line stopped by the box*, listing rejected modules if any. Clearing with a Supervisor PIN clears it on the console too. |
| 5.6 | Clear a hold with the network cable pulled. | *Hold not cleared: the box is not answering*. Reconnect and clear. |
| 5.7 | Abort a batch after programming but before the QR scan (Supervisor abort), then run normal batches. | No hold: the Box does not look for unreported addresses. |

## 6. Power cut (requirement R3)

| # | Steps | Expected |
|---|---|---|
| 6.1 | Switch the station PC off at the wall during **PROGRAMMING MODULES**. Restart. | The app starts; the interrupted batch's addresses are quarantined (never reused); a new batch gets new addresses. |
| 6.2 | Switch off just after **BATCH COMPLETE**, before the report could be sent (cable pulled first). Restart, reconnect, log in. | The batch is reported after the login. |
| 6.3 | Switch off right after a block switch (2.3) before reconnecting. Restart. | The promote is sent before any new block request (console shows the old block closed). |

## 7. Restart the tray

Note each slot's MAC on screen before and after every restart.

| # | Steps | Expected |
|---|---|---|
| 7.1 | **Stock + Pricol**, 4 modules. Run to **SCAN MODULE QR**, scan slot 1, then cover slot 2's label (unreadable). Press **RESTART TRAY**, uncover or replace the label, run the batch to **BATCH COMPLETE**. | After the restart the headline is **PREPARE FOR STOCK PROGRAMMING** and the instruction says *Tray restarted 1x*. Same temporary MACs, and each slot gets the **same Pricol MAC** as in the first run. Slot 1's QR is accepted again without a hold. Box console: 4 modules with those Pricol MACs; the Pricol block shows only 4 addresses used. |
| 7.2 | **Stock only**, press **RESTART TRAY** during **PROGRAMMING MODULES**. | **STOPPING...**, then the batch is back at the first step within a few seconds; the MPCLI processes have ended (Task Manager). Running again programs the same Stock MACs; all modules pass. |
| 7.3 | Restart during **TESTING MODULES** (RF test) and during **RESET ALL MODULES NOW** (APP test). | Both stop and restart cleanly; the Golden Rig accepts the next RF test of the same tray. |
| 7.4 | Stock + Pricol: on purpose, press **RUN APP TEST** without switching to application mode, so the modules fail. Then **RESTART TRAY** and do it right. | The second run passes; the report carries only the second run's results. |
| 7.5 | Leave slot 2 unseated (**NOT DETECTED - skipped**), seat it, **RESTART TRAY**. | Slot 2 runs in the restarted batch with a MAC address; the other slots keep theirs. |
| 7.6 | Close the app (or switch the PC off) after a restart during the Pricol steps; start again. | No crash. The unfinished tray is not resumed; addresses left mid-programming are quarantined, as in 6.1. |

## 8. Known items to watch

- The bench tool's automated tests pass, but their test process does not exit on its own (a thread left running; this predates the Box work). On the real jig, check that the bench tool closes cleanly after **RESTART WORKFLOW** during programming (guide F3), and report it if the window closes but the process stays in Task Manager.
