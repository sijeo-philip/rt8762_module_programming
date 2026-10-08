# Production Workflow

The Station Application shall guide the Supervisor and Operator through a fixed, non-skippable production workflow. The user interface should clearly indicate the current required action, for example by highlighting the active step and enabling only the controls that are valid at that stage.

The objective is to make the production sequence self-explanatory and to minimize the possibility of operator error.

## 1. Supervisor Starts Session

The Supervisor logs in and starts a production session.

During session setup, the Supervisor:

- Identifies the production jig.
- Binds detected COM ports to physical jig slots.
- Confirms the jig configuration.
- Starts the controlled production session.

The COM-port-to-slot binding remains valid for the active session unless explicitly changed by an authorized Supervisor.

---

## 2. Operator Login / Badge

The Operator logs in using the supported login or badge mechanism.

The Operator then configures the batch to be processed on the DUT test jig.

The batch setup includes:

- Batch identification.
- Stock firmware path.
- Pricol firmware path.
- Required programming mode.

The programming mode may be:

- **Stock only**
- **Stock + Pricol**

If **Stock only** is selected, the Pricol programming and functional-test stages are skipped as described later in this workflow.

---

## 3. Jig Auto-Detection

The application automatically detects the connected jig and associated interfaces.

The detected hardware is compared against the Supervisor-approved session configuration.

---

## 4. Configuration Auto-Validation

Before production can start, the application validates the complete station configuration.

The validation includes, but is not limited to:

- Number of jig slots.
- Number of detected COM ports.
- COM-port-to-slot bindings.
- Jig identity.
- LAN server connectivity.
- Stock firmware configuration.
- Pricol firmware configuration.
- Programming mode.
- Required station resources and services.

The application shall not allow the batch to continue if any mandatory configuration is invalid.

---

## 5. Authorized MAC Cache Check

The application checks the local authorized MAC-address cache for both:

- Stock MAC addresses.
- Pricol production MAC addresses.

If the available quantity falls below the configured threshold, the Station Application requests additional authorized MAC-address blocks from the LAN Server.

---

## 6. Import Authorized MAC Blocks Locally

Authorized MAC-address blocks received from the LAN Server are validated and imported into the local station database/cache.

Only MAC addresses belonging to a valid authorized block may be reserved for programming.

---

## 7. Ready

Once all configuration, allocation and hardware checks are successful, the station enters the **READY** state.

The UI clearly indicates that modules may now be loaded into the jig.

---

## 8. Load Modules

The Operator loads modules into the production jig.

The jig may support 4 or 8 physical slots, but all slots do not have to be populated for every batch.

The Station Application shall detect or allow confirmation of which physical slots are populated and shall treat each slot independently.

For example, an 8-slot jig may contain modules only in:

```text
Slot 1
Slot 2
Slot 4
Slot 7
```

while the remaining slots are empty.

Empty slots shall be clearly shown in the UI and shall not block the populated slots from being processed.

The application uses the previously established physical slot bindings to associate each populated module position with its programming interface.

---

## 9. Start

The Operator starts the batch.

After START is accepted, the application controls the production workflow and prevents steps from being executed out of sequence.

---

## 10. Reserve Stock MAC Addresses

One Stock MAC address is reserved from the local authorized Stock MAC cache for each occupied jig slot.

The reservation is recorded in the local station database before programming begins.

A reserved MAC must not be silently reassigned if programming later becomes uncertain.

---

## 11. Stock Programming

All populated slots are programmed with the Stock firmware.

The number of parallel programming subprocesses is equal to the number of populated and active/bound jig slots.

For example:

```text
8-slot jig with 8 modules
        ↓
8 parallel MPCLI subprocesses
```

or:

```text
8-slot jig with modules only in Slots 1, 2, 4 and 7
        ↓
4 parallel MPCLI subprocesses
```

Each programming operation follows the validated MPCLI sequence:

```text
Flash Stock firmware
        ↓
Wait for operation completion
        ↓
Program reserved Stock MAC
        ↓
Read back device identity
```

Programming results are tracked independently for every slot.

### Per-Slot Programming Progress Display

While firmware programming and MAC writing are in progress, the Operator UI shall display one progress/status panel for every physical slot in the jig.

For an 8-slot jig, the UI shall show eight slot panels or progress bars:

```text
Slot 1  [##########]  Firmware Programmed / MAC Written
Slot 2  [######....]  Programming
Slot 3  [..........]  Empty
Slot 4  [##########]  Firmware Programmed / MAC Written
Slot 5  [..........]  Empty
Slot 6  [###.......]  Writing MAC
Slot 7  [##########]  Firmware Programmed / MAC Written
Slot 8  [..........]  Empty
```

Each slot shall have an independent state such as:

- EMPTY
- READY
- PROGRAMMING FIRMWARE
- FIRMWARE PROGRAMMED
- WRITING MAC
- MAC WRITTEN
- READBACK VERIFYING
- PASS
- FAIL
- HOLD
- UNCERTAIN

The progress indication shall reflect the actual status of that slot and shall not imply that the complete batch succeeded merely because some slots succeeded.

A failed or empty slot shall not prevent other successfully programmed slots from completing their programming operations.

---

## 12. Stock MAC Readback

After Stock programming, the application reads the MAC address back from each successfully programmed DUT using MPCLI.

For every populated slot that reached the MAC-written stage:

```text
Allocated Stock MAC
        ↓
Programmed Stock MAC
        ↓
MPCLI Readback
        ↓
Compare
```

The readback MAC must match the MAC reserved for that slot.

A mismatch, unreadable MAC, ambiguous result, timeout, or uncertain programming state prevents that particular slot from being accepted as successfully programmed.

The result of one slot shall not automatically invalidate other slots.

Only slots that have successfully completed Stock programming and Stock MAC readback shall be eligible to proceed to the Stock RF Test.

---

## 13. Stock RF Test

After Stock programming and readback verification, only the successfully verified Stock MAC addresses are transmitted over the LAN/Wi-Fi connection to the Golden Module Test Jig.

Slots that are empty, failed, uncertain, or did not complete MAC verification are excluded from the RF-test request.

The Golden Module Test Jig performs the RF verification for each DUT by:

1. Receiving the expected Stock MAC address.
2. Connecting to the corresponding DUT over Bluetooth.
3. Confirming successful connection.
4. Disconnecting from the DUT.
5. Reporting the result back to the Station Application.

The Station Application records the RF result for each slot as:

- PASS
- FAIL
- HOLD, where applicable

The RF test result is stored in the local station database.

---

## 14. Reserve Pricol MAC Addresses

If the batch is configured for **Stock + Pricol**, one permanent Pricol MAC address is reserved from the local authorized Pricol MAC cache only for each slot that successfully completed the required Stock programming, Stock readback, and Stock RF-test stages.

Slots that failed earlier stages are not automatically assigned a Pricol MAC and do not block valid slots from continuing.

The Pricol MAC must be different from the temporary Stock MAC assigned to the same module.

---

## 15. Pricol Programming

All eligible slots are programmed with the Pricol firmware.

As with Stock programming, the number of parallel MPCLI subprocesses is equal to the number of eligible populated slots, not necessarily the total physical slot count.

For example:

```text
Slot 1 ── MPCLI subprocess 1
Slot 2 ── MPCLI subprocess 2
Slot 3 ── MPCLI subprocess 3
...
Slot 8 ── MPCLI subprocess 8
```

Each slot performs the validated programming sequence independently.

The Operator UI shall again show independent per-slot progress bars/status indicators during:

- Pricol firmware flashing.
- Pricol MAC writing.
- Pricol MAC readback verification.

A failure on one slot shall not stop another valid slot from completing its Pricol programming sequence.

---

## 16. Pricol MAC Readback

After Pricol programming, the application reads the programmed MAC address back from each DUT.

For every slot:

```text
Reserved Pricol MAC
        ↓
Programmed Pricol MAC
        ↓
MPCLI Readback
        ↓
Compare
```

The readback MAC must exactly match the Pricol MAC reserved for that module.

A mismatch is treated as an identity failure and the affected module must not proceed as PASS.

Other slots whose Pricol MAC readback matches correctly may continue to the Functional Test.

---

## 17. Functional Test

After successful Pricol programming and MAC verification, the Station Application performs the required functional tests only on the slots that remain eligible.

A defined sequence of AT commands is issued to each module to validate the Pricol firmware.

Each command result is recorded independently for every slot.

The functional-test stage produces a PASS or FAIL result based on the configured acceptance criteria.

---

## 18. QR Scan

After programming and testing are complete, the Operator scans the QR code printed on each module that has successfully completed the required workflow stages.

Slots that are empty, failed, HOLD, or otherwise ineligible are skipped automatically.

The scan order is fixed by ascending eligible slot number.

For a fully populated and successful 8-slot batch:

```text
Slot 1
  ↓
Slot 2
  ↓
Slot 3
  ↓
...
  ↓
Slot 8
```

For a partially populated or partially successful batch, the application skips ineligible slots automatically.

For example, if only Slots 1, 2, 4 and 7 are eligible:

```text
Slot 1
  ↓
Slot 2
  ↓
Slot 4
  ↓
Slot 7
```

The Operator does not manually select the destination slot.

The Station Application always knows which eligible slot is expected next and binds the scanned QR code to that slot.

The Operator shall not be required to manually choose or skip slots during QR scanning.

The application shall reject:

- Duplicate QR codes.
- Out-of-sequence scans.
- Invalid QR data.
- Scans when the workflow is not in the QR-scanning state.

---

## 19. Final Validation

Before the batch can be completed, the application validates that all required production records exist for every slot.

The final validation includes, as applicable:

- Stock MAC reservation.
- Stock programming result.
- Stock MAC readback.
- Stock RF test.
- Pricol MAC reservation.
- Pricol programming result.
- Pricol MAC readback.
- Functional-test result.
- QR-code binding.

Each populated slot/module is recorded independently in the local station database with its final production disposition:

- **PASS**
- **FAIL**
- **HOLD**

Empty physical slots are recorded or represented as unpopulated/unused for that batch and are not treated as production failures.

No module may be recorded as PASS if a mandatory operation or verification step is missing or unsuccessful.

---

## 20. Queue Server Upload

At the end of the batch, after confirmation by the Operator, the completed batch records are committed to the local station database and queued for upload to the LAN Server.

Server upload shall not block preparation of the next batch.

If the LAN Server is temporarily unavailable:

```text
Batch completed locally
        ↓
Upload queued
        ↓
Production may continue
        ↓
Upload retries later
```

The original production timestamps and station identity must be preserved.

---

## 21. Auto-Prepare Next Batch

After the current batch is completed, the application automatically prepares the station for the next batch.

The Operator:

1. Unloads the tested modules.
2. Loads the next set of modules.
3. Starts the next batch.

The applicable production workflow is then repeated.

---

# Session Completion and Server Synchronization

At the end of the complete production session, the local station database is synchronized with the LAN Server.

The synchronization includes:

- Completed batch records.
- Programming history.
- MAC-address consumption.
- Test results.
- QR associations.
- PASS / FAIL / HOLD dispositions.
- Audit records.
- Pending uploads.

The Supervisor closes the production session only after the required session-level checks are completed.

---

# Stock-Only Programming Mode

If the Operator selects **Stock Only** during Step 2, the following Pricol-specific stages are skipped:

```text
Step 14 - Reserve Pricol MAC
Step 15 - Pricol Programming
Step 16 - Pricol MAC Readback
Step 17 - Functional Test
```

The workflow therefore becomes:

```text
Session Setup
    ↓
Stock Programming
    ↓
Stock Readback
    ↓
Stock RF Test
    ↓
QR Scan
    ↓
Final Validation
    ↓
Queue Upload
    ↓
Next Batch
```

---

# Full Stock + Pricol Programming Mode

If the Operator selects **Stock + Pricol**, the complete production sequence is executed:

```text
Supervisor Session Setup
        ↓
Operator Login / Batch Setup
        ↓
Jig Detection
        ↓
Configuration Validation
        ↓
Authorized MAC Cache Check
        ↓
Import Authorized MAC Blocks
        ↓
READY
        ↓
Load Modules
        ↓
START
        ↓
Reserve Stock MACs
        ↓
Stock Programming
        ↓
Stock Readback
        ↓
Stock RF Test
        ↓
Reserve Pricol MACs
        ↓
Pricol Programming
        ↓
Pricol Readback
        ↓
Functional Test
        ↓
QR Scan
        ↓
Final Validation
        ↓
Queue Server Upload
        ↓
Prepare Next Batch
```

---

# Partial Population and Per-Slot Continuation

The production workflow is slot-based rather than all-or-nothing at batch level.

An 8-slot jig does not require all eight slots to be populated, and a batch is not required to stop merely because one or more slots fail.

Each physical slot shall be tracked independently from batch start through final disposition.

For example:

```text
Slot 1  PASS
Slot 2  PASS
Slot 3  EMPTY
Slot 4  PROGRAMMING FAIL
Slot 5  EMPTY
Slot 6  PASS
Slot 7  HOLD
Slot 8  PASS
```

In this example:

- Slots 1, 2, 6 and 8 may continue to the next eligible workflow stage.
- Slot 3 and Slot 5 remain EMPTY.
- Slot 4 is recorded as FAIL and does not proceed.
- Slot 7 is recorded as HOLD and does not proceed without authorized disposition.

The application shall therefore maintain an eligibility set for each workflow stage.

Conceptually:

```text
Loaded Slots
    ↓
Successful Stock Programming Slots
    ↓
Successful Stock Readback Slots
    ↓
Successful Stock RF Test Slots
    ↓
Successful Pricol Programming Slots
    ↓
Successful Pricol Readback Slots
    ↓
Successful Functional Test Slots
    ↓
QR-Eligible Slots
    ↓
Final PASS / FAIL / HOLD
```

A slot may leave the active flow because of FAIL, HOLD, UNCERTAIN, or EMPTY state without forcing unrelated successful slots to stop.

The final batch record may therefore contain a mixture of PASS, FAIL, HOLD, and EMPTY slot outcomes.

---

# Per-Slot Progress and Status Requirements

The Operator UI shall provide a clearly visible status/progress indication for every physical slot.

For an 8-slot jig, eight slot indicators shall always be visible so the Operator can immediately understand the state of the jig.

During programming, each slot indicator should show both progress and the current action.

Example:

```text
Slot 1  [##########]  PASS - MAC 01:02:03:04:05:06
Slot 2  [#####.....]  Programming Firmware
Slot 3  [..........]  EMPTY
Slot 4  [########..]  Writing MAC
Slot 5  [..........]  EMPTY
Slot 6  [##########]  PASS - MAC Readback Verified
Slot 7  [##########]  FAIL - MAC Readback Mismatch
Slot 8  [###.......]  Programming Firmware
```

The slot indication shall remain available during:

- Stock firmware programming.
- Stock MAC writing.
- Stock MAC readback.
- Stock RF testing.
- Pricol firmware programming.
- Pricol MAC writing.
- Pricol MAC readback.
- Functional testing.
- QR scanning.
- Final validation.

The UI should make successful, failed, hold, uncertain, empty, and in-progress slots visually distinct.

Only eligible slots shall be enabled for the next workflow stage.

---

# Operator UI Behaviour

The Station Application UI shall actively guide the Operator through the production process.

The workflow must not depend only on the Operator remembering the correct sequence.

At any point in time, the UI should clearly indicate:

- The current production step.
- The next required action.
- Which physical slot is active, where applicable.
- Whether Operator action is required.
- Whether the station is waiting for an automatic operation to complete.
- PASS / FAIL / HOLD status for each slot.
- Any condition that prevents the workflow from continuing.

Recommended UI behaviour includes:

- Highlighting the active step.
- Enabling only the button required for the current state.
- Disabling operations that are not currently valid.
- Providing clear plain-language instructions.
- Automatically advancing after successful automatic operations.
- Preventing steps from being skipped.
- Preventing repeated programming unless explicitly allowed by the workflow.
- Requiring Supervisor intervention for HOLD, uncertain programming, configuration changes, or exceptional recovery.

The production workflow shall be enforced by the application state/service layer and not only by disabled GUI buttons.

---

# Simplified Production State Flow

```text
SUPERVISOR STARTS SESSION
        ↓
OPERATOR LOGIN / BATCH SETUP
        ↓
JIG AUTO-DETECTED
        ↓
CONFIG AUTO-VALIDATED
        ↓
AUTHORIZED MAC CACHE CHECK
        ↓
IMPORT AUTHORIZED MAC BLOCKS
        ↓
READY
        ↓
LOAD MODULES
        ↓
START
        ↓
STOCK MAC RESERVED
        ↓
STOCK PROGRAMMING
        ↓
STOCK MAC READBACK
        ↓
STOCK RF TEST
        ↓
[ STOCK ONLY? ]
      /       \
    YES        NO
     │          ↓
     │     PRICOL MAC RESERVED
     │          ↓
     │     PRICOL PROGRAMMING
     │          ↓
     │     PRICOL MAC READBACK
     │          ↓
     │     FUNCTIONAL TEST
     │          │
     └──────────┘
          ↓
       QR SCAN
          ↓
   FINAL VALIDATION
          ↓
   PASS / FAIL / HOLD
          ↓
   QUEUE SERVER UPLOAD
          ↓
   PREPARE NEXT BATCH
```

## Important Production Rules

- Only authorized MAC addresses may be programmed.
- Stock and Pricol MAC pools are maintained separately.
- Stock and Pricol MAC addresses assigned to the same module must be different.
- A MAC involved in uncertain programming must not be automatically returned to the available pool.
- Programming, readback and test results are maintained independently for each physical slot.
- All physical slots do not have to be populated for a batch.
- Empty slots are allowed and do not cause the batch to fail.
- A failure on one slot does not automatically stop successfully progressing slots.
- Only slots that satisfy the previous stage's acceptance criteria are eligible for the next stage.
- Multiple DUTs may be programmed in parallel, but each slot retains its own subprocess, progress indication, result and traceability record.
- The UI shall show one independent progress/status indicator per physical slot during programming and verification activities.
- QR scanning shall proceed only through eligible slots, in ascending slot order, automatically skipping empty, failed or HOLD slots.
- A successful MPCLI process return alone is not sufficient identity verification; the written MAC must be read back and compared.
- Stock firmware requires both MAC readback and RF confirmation.
- Pricol firmware requires MAC readback and functional AT-command testing.
- QR codes are bound only after the required programming and testing stages are complete.
- Production history is append-only; retries and rework create additional records rather than overwriting previous results.
- Local production may continue while completed records are waiting to synchronize with the LAN Server, subject to availability of authorized local MAC allocations.
- Finally the Application have to converted to installable application so that it can be installed on each test Jig/station