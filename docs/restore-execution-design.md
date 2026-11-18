# TaimoBot controlled restore and rollback design

## Status

Design only. This document does not authorize database replacement, sidecar deletion, or live trading. TaimoBot remains paper-trading only. The current implementation supports recovery inventory, isolated staging, immutable SHA-256 fingerprints, explicit approval, read-only execution preflight, process-wide restore locking, and operator-reviewed abandoned-marker recovery. It does not yet implement live database replacement or rollback.

## Non-negotiable invariants

1. Never execute a restore unless an administrator-approved plan is still fresh and all three artifacts (source, staged candidate, and pre-restore rollback backup) pass integrity, schema, foreign-key, path, file-type, and fingerprint checks immediately before execution.
2. Keep the live database and its sidecars unchanged until the system has exclusive coordination and has recorded a durable execution-start audit event.
3. Never unlink, rename, truncate, or casually checkpoint a live `-wal` or `-shm` file. Their contents may be part of the current database state.
4. The stable `.restore.guard` file must never be removed or replaced. The `.restore.lock` marker is separate and is only removed by its token-owning controlled path or the explicit abandoned-marker recovery helper.
5. A process-level flock coordinates only participants that use the TaimoBot guard protocol. It does not stop arbitrary SQLite clients or unrelated processes. The operator must stop all backend workers and any external process using the database before execution.
6. No endpoint may report success merely because a file rename succeeded. The installed database must pass post-install integrity, required-schema, and foreign-key checks before the plan can be marked APPLIED.
7. If installation or post-install verification fails, rollback must be attempted from the independently verified pre-restore artifact. If rollback cannot be verified, keep the application stopped and require operator intervention.
8. Restore execution must not alter trading mode, enable live execution, or bypass emergency-stop state.

## Required state machine

`PREPARED -> APPROVED -> EXECUTING -> APPLIED`

Failure paths:

- `EXECUTING -> ROLLING_BACK -> ROLLED_BACK` when the source install fails but the rollback artifact is successfully installed and verified.
- `EXECUTING -> FAILED` only when no database replacement has occurred and the live database remains verified unchanged.
- `EXECUTING -> RECOVERY_REQUIRED` when the system cannot prove which database image is installed or cannot verify rollback.
- `APPROVED -> EXPIRED` when the approved plan exceeds its allowed age before execution begins.

Do not overload the existing FAILED status to mean both “nothing changed” and “rollback uncertain.” Those outcomes have materially different operational meanings. State transitions must be conditional updates fenced by plan ID and execution token.

## Preconditions before implementation can execute

- All backend workers and schedulers using the database are stopped. This is an operator-enforced prerequisite, not something flock alone can prove for arbitrary clients.
- No external process has the database open.
- The live database, source backup, staged candidate, and rollback backup are on the expected managed filesystem locations; all are regular non-symlink files.
- The source, staged, and rollback SHA-256 values still match the immutable fingerprints captured at preparation.
- Source, staged, rollback, and live database each pass integrity, required-schema, and foreign-key checks as applicable.
- Live database sidecars have been inventoried. If WAL/SHM exist, execution must stop unless a separately tested, lossless handling procedure proves how their state is incorporated and preserved.
- A durable audit destination is available and writable. Audit STARTED must be fsynced before any live-file mutation.
- An exclusive restore guard is held, with no application-managed database connections remaining.
- A second operator confirmation is recorded at execution time; approval is not a substitute for the execution confirmation.

## File installation strategy to investigate and test

A single `os.replace` can atomically replace one directory entry on the same filesystem. It cannot atomically replace a database plus its WAL/SHM sidecars as a group. Therefore a naïve rename of the staged database over the live database is prohibited.

Implementation research must choose and test one explicit strategy:

- Prefer a quiescent, checkpointed, standalone SQLite database image with no live WAL/SHM dependency, but only after a supported SQLite procedure has safely incorporated all committed WAL content and verified the result.
- Preserve pre-operation live database and sidecar artifacts in a uniquely named, fsynced forensic/rollback directory before changing any names.
- Build the replacement file in the same filesystem as the live database; fsync the file and parent directory before publication.
- Use a recoverable rename sequence with a durable journal that makes every intermediate state unambiguous after process or machine crash.
- Never delete the old live image until the new image is verified and the rollback window is explicitly closed by a later, separate operation.

The final protocol must document exactly what happens if the process stops after each filesystem operation. It must not describe a multi-file sequence as atomic unless crash testing demonstrates that claim.

## Implemented journal foundation

The current code now provides an independent JSONL restore execution journal in
`backend/app/maintenance.py`:

- `append_restore_execution_event(...)` appends an allow-listed event with
  sequence number, SAST timestamp, plan ID, execution token, operator, incident
  reference, and bounded JSON details.
- Each record includes the previous record hash and its own SHA-256 digest.
  Appends use an exclusive file lock, complete-write loop, and `fsync`; readers
  take a shared file lock so inspection cannot mistake an in-progress append for
  corruption. Creation also fsyncs the containing directory.
- `verify_restore_execution_journal(...)` checks sequence continuity, event
  allow-list, previous-hash links, record digests, and complete newline-terminated
  records. Appending is refused if existing history fails verification.
- The journal is independent of SQLite and can therefore be written while the
  application database is locked.

This is an integrity check against accidental corruption and partial writes,
not a cryptographic signature or protection against an attacker who can rewrite
the entire journal and recompute every hash. A future execution design should
anchor the terminal journal digest in a separately controlled system or
write-once store. The helper records events only: no endpoint invokes it to
replace the live database, and no restore execution is implemented.

## Implemented read-only crash-state inspection

The journal foundation now includes `inspect_restore_execution_state()`. It
verifies the journal before classifying events by plan ID and execution token.
It distinguishes an interrupted start, a published-but-unconfirmed image, an
incomplete rollback, a recorded recovery-required outcome, and terminal
complete/rolled-back events that still need reconciliation against the actual
database files.

The inspector also enforces execution identity and chronology: all events
under one plan/execution token must retain the same operator and incident
reference, and timestamps must be timezone-aware ISO-8601 values in
non-decreasing order. Contradictory identity or time ordering invalidates the
execution history and requires operator review.

The inspector also checks event ordering and rejects semantically impossible
transitions, even when the hash chain itself is valid. It is read-only and
always returns `automatic_action: NONE`; it never resumes, retries, rolls back,
changes plan status, or touches the live database. A terminal journal event is
not proof that the on-disk database currently matches that event. An operator
must independently inspect the live database, sidecars, preserved images,
fingerprints, and restore plan before deciding what to do.

The inspector now also reports event-specific evidence completeness. The
minimum evidence contract is:

- `RESTORE_EXECUTION_STARTED`: SHA-256 fingerprints for live database, source
  backup, staged candidate, and rollback backup under `details.artifacts`.
- `RESTORE_SOURCE_PUBLISHED`: staged-candidate fingerprint and a distinct
  `published_database_sha256` under `details.artifacts`. This is deliberately
  separate from the pre-restore `live_database_sha256` recorded at STARTED;
  the two fingerprints describe different database states and must not be
  treated as conflicting versions of one artifact.
- `RESTORE_POSTCHECK_PASSED`: `integrity_check: "ok"`,
  `foreign_key_violations: 0`, `schema_check: "passed"`, and the installed
  database SHA-256.
- `RESTORE_ROLLBACK_STARTED`: rollback-backup SHA-256.
- `RESTORE_ROLLBACK_VERIFIED`: integrity result, zero foreign-key violations,
  and restored-database SHA-256.
- `RESTORE_EXECUTION_COMPLETE`: final database SHA-256.
- `RESTORE_EXECUTION_ROLLED_BACK`: restored database SHA-256.
- `RESTORE_EXECUTION_RECOVERY_REQUIRED`: a non-empty reason.

The inspector surfaces missing or malformed evidence as
`INCOMPLETE_REQUIRES_OPERATOR_REVIEW` per execution and lists the affected
events and fields. Existing journal records remain readable for forensic
continuity, but absent evidence is never inferred or upgraded to verified.
This check is advisory/read-only; it does not prove the underlying file matches
the recorded hash. The separate artifact reconciler must still be run, and
database-state verification remains explicitly false.

## Implemented read-only artifact evidence reconciliation

`reconcile_restore_execution_artifacts(...)` compares four current files against
SHA-256 fingerprints recorded in the selected plan/execution journal:
`live_database`, `source_backup`, `staged_candidate`, and `rollback_backup`.
For each file it reports whether a fingerprint was recorded and whether the
current bytes match. It also runs SQLite integrity and foreign-key checks and
inventories live-database WAL/SHM sidecars. During each SQLite check it
keeps the original no-follow file descriptor open, verifies the pathname still
resolves to the same regular-file identity before and after SQLite opens the
path read-only, and re-hashes the original descriptor after the check. A path
replacement or in-place mutation during this inspection window is surfaced as
unsafe and review-required. This narrows a pathname/file race; it does not
eliminate hostile parent-directory substitution or provide a filesystem-wide
atomic snapshot. Managed-directory validation and operator quiescence remain
required.

A missing recorded fingerprint, changed file, unsafe/missing path, failed
integrity check, foreign-key violation, or present live sidecar keeps the result
in review-required state. This helper is read-only: it does not repair, copy,
replace, checkpoint, truncate, or remove files. Even a complete fingerprint
match returns `database_state_verified: false`; a main database file hash by
itself cannot prove a consistent SQLite state when WAL/SHM sidecars exist, nor
can a hash establish that the application-level data is correct. Operator
verification and a separately tested quiescence protocol remain mandatory.

The checker opens artifact files with no-follow semantics and requires regular
files. Conflicting fingerprints for the same artifact across events are treated
as contradictory evidence, not resolved by choosing the latest value. If the
live database has WAL/SHM sidecars, the checker inventories them and deliberately
skips opening that database through SQLite; the sidecars are preserved and the
overall result remains review-required. A symlinked or missing artifact also
fails closed.

To make this evidence useful during future execution, every execution event
that establishes an artifact should record its SHA-256 under
`details.artifacts` using the exact keys `live_database_sha256`,
`source_backup_sha256`, `staged_candidate_sha256`, and
`rollback_backup_sha256`. Older journal entries without those fields remain
inspectable but cannot be declared fingerprint-matched.

## Audit requirements

Append-only, fsynced events should include:

- `RESTORE_EXECUTION_STARTED`: plan ID, execution token, operator, incident reference, timestamp, selected source fingerprint, staged fingerprint, rollback fingerprint, and live database identity.
- `RESTORE_SOURCE_PUBLISHED`: file identities and hashes after publication.
- `RESTORE_POSTCHECK_PASSED`: integrity/schema/foreign-key outcomes and installed hash.
- `RESTORE_ROLLBACK_STARTED` and `RESTORE_ROLLBACK_VERIFIED` when rollback is needed.
- `RESTORE_EXECUTION_COMPLETE`, `RESTORE_EXECUTION_ROLLED_BACK`, or `RESTORE_EXECUTION_RECOVERY_REQUIRED` as the terminal event.

If a terminal audit write fails, the operation must be treated as requiring operator review, even if a database image appears valid.

## Crash and fault-injection test matrix

Before enabling execution, tests must inject failure after every durable boundary:

1. Before execution audit.
2. After STARTED audit but before any file mutation.
3. After live DB/sidecar preservation.
4. After replacement staging file fsync.
5. After first rename.
6. After replacement publication.
7. During post-install integrity check.
8. During schema and foreign-key verification.
9. During rollback publication.
10. During rollback verification.
11. During terminal audit write.
12. With disk full, permission denied, symlink substitution, path replacement, and process termination.
13. With an active app connection in another process and with an unrelated SQLite process holding the DB.
14. With WAL and SHM present, absent, malformed, or changing.

Each test must prove either (a) the original live database remains intact, (b) the new database is fully verified and the plan is APPLIED, or (c) the service remains stopped with a RECOVERY_REQUIRED outcome and sufficient preserved artifacts for manual recovery. No ambiguous “success” is acceptable.

## Operator runbook requirements

- Stop all backend workers, schedulers, and external database users.
- Preserve the live database, WAL, SHM, and stable guard file before intervention.
- Inspect the restore marker and guard state; never auto-reclaim an abandoned marker.
- Confirm the selected plan, operator identity, incident reference, and current paper-only mode.
- Run preflight immediately before execution.
- Execute only through a separately implemented, audited operator path.
- Verify terminal audit event, installed database integrity/schema/foreign keys, plan state, and emergency-stop state before restarting any worker.
- If any state is uncertain, keep the service stopped. Do not retry blindly or manually delete sidecars/markers.

## Explicit non-goals

This design does not implement or authorize restore execution, database swapping, rollback execution, WAL/SHM deletion, live trading, or automatic abandoned-lock recovery. Those require separate implementation, fault-injection tests, and operator review.
