SDK version 7.0.0

# Station Application Requirements

This is the appendix to [DEVELOPER-GUIDE.md](DEVELOPER-GUIDE.md). It lists the rules a production station application keeps, for design reviews and acceptance testing. The guide explains the calls; this document states what must hold. Each rule is testable.

The rules are numbered afresh for 6.0. The 5.x rules about verify calls, gates, commits, cursor reports and status polling no longer apply and are not carried over.

## 1. The one invariant

> **A MAC address is never programmed into two different modules.**

An address may be *skipped* (never used), for example after a crash. That is acceptable; when a report jumps past unreported addresses in the current block, the box puts the station on hold so a supervisor can look. An address being *re-used* is never acceptable. When the two conflict, the application leaves an address unused rather than risk using it twice, and it stops with a clear message rather than invent an address it does not own.

The box backs this up: it rejects an address that is already on another module, or that was never given to this station, and puts the station on hold. That is a safety net, not a substitute for the rules below.

## 2. Saved state

**R1 — What survives a crash.** These survive a process crash, power loss and restart:

- the current block (id, first and last address, master range);
- the reserve block, if one is held;
- the position reached in the current block (how many addresses are used);
- the promote still owed to the box, if any;
- finished modules not yet reported (QR, MAC, passed, note, batch ID).

**R2 — One document, one atomic write.** All of R1 is written together as one document in one crash-safe write: never split across files written separately.

- Windows: write a temporary file, `FlushFileBuffers`, then `ReplaceFile`.
- POSIX: write a temporary file, `fsync`, `rename`, then `fsync` the directory.

**R3 — Acceptance is a power-cut test.** Power is pulled during the write, many times. The saved document is always the old one or the new one, intact.

## 3. Using a block

**R4 — Save the position before programming.** Before any address of a tray is programmed, the position has already been moved past every address of that tray and saved (R2). The saved position is always at or ahead of what has been programmed, never behind. A crash can then only leave addresses unused.

**R5 — Only addresses from the station's own blocks.** Every address programmed comes from the current block (or, after switching, the former reserve), counted upwards from `first_mac`, and is checked to be no higher than `last_mac` before it is programmed.

**R6 — A tray never spans two blocks.** When fewer addresses remain than the tray needs, the application either runs a short tray or switches block and leaves the remainder unused.

**R7 — The right master range.** Every `request_block()` names the master range of the product being built. The box never substitutes another one.

## 4. Reserve and switching

**R8 — One reserve at a time.** The application requests a reserve when the current block runs low, and does not request another while one is held.

**R9 — Switching is one saved write.** Moving from the current block to the reserve, resetting the position, and recording the owed promote happen in one write (R2).

**R10 — An owed promote is sent before any new block request.** It survives a restart and is re-sent until the box answers.

## 5. Reporting

**R11 — Report once a tray is fully finished.** Every module that had an address programmed into it is reported, passed or failed, once the station's whole process for it is done, with the production batch ID it belongs to (required; the operator cannot start a tray without one).

**R12 — Unsent modules are kept, not dropped.** After `ClientBusy`, `BoxUnreachable` or `SessionExpired`, the modules stay in the saved state and go out in a later report (after the operator logs in again, for `SessionExpired`). Re-sending modules the box may already have is safe.

**R13 — The line does not wait on the box.** Reports are sent without stopping the jig (normally from a worker thread). While the box is unreachable the line keeps running on the current block and then the reserve; when both are used up it stops with an operator message and never invents an address.

## 6. Holds

**R14 — A hold stops new trays.** When a report answers `on_hold`, the application starts no new tray until the hold is cleared. Trays already in progress may finish and be reported. Rejected modules are shown to the operator (QR, MAC, reason in plain words) and kept apart.

**R15 — A hold survives a restart.** The application saves that it is on hold and opens the hold screen again after a restart.

**R16 — Only the supervisor's phone and PIN clear a hold at the station.** The hold screen asks for them and calls `clear_hold()`; the line restarts only on `True`.

## 7. Lost or copied state

**R17 — Redraw, never resume.** If the saved state is lost, damaged, restored from a backup or copied from another PC, the application abandons every block it held, gets a fresh block, and never rebuilds a position from any source (guide 4.11). A restored backup does not know the addresses programmed since it was taken.

## 8. Acceptance checklist

- [ ] Power-cut durability (R2, R3).
- [ ] Crash restart: no address re-used; at most one tray's addresses left unused (R1, R4).
- [ ] Position saved before every tray (R4).
- [ ] No tray spans two blocks; every address within its block (R5, R6).
- [ ] Right master range on every block request (R7).
- [ ] One reserve at low water; switching and owed promote in one write; owed promote re-sent after restart (R8–R10).
- [ ] Every programmed module reported once, passed or failed (R11).
- [ ] Box unplugged: line runs on through the reserve, modules kept and reported once the box returns, stop with a message when addresses run out (R12, R13).
- [ ] Rejected module stops new trays, survives restart, cleared only by a supervisor's phone and PIN (R14–R16).
- [ ] Copied or restored state redraws (R17).

## 9. Points for the application team to confirm

1. Is the module's address store one-time-programmable? (Sets the cost of an unused address — R4.)
2. Can the jig run a short tray at the end of a block? (R6.)
3. The measured save time per tray on the real hardware (R2, R4).
