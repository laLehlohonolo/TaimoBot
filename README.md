# TaimoBot
it's a trading bot called TaimoBot, designed to help users with Forex trading by providing accurate predictions.


## Global emergency-stop administration

The global emergency-stop activation and resume endpoints require an authenticated
user whose email is explicitly listed in the backend environment variable
`TAIMOBOT_ADMIN_EMAILS`. Configure one or more administrator email addresses
as a comma-separated list, for example:

```text
TAIMOBOT_ADMIN_EMAILS=operator@example.com,owner@example.com
```

The allowlist is empty by default, so ordinary registered users cannot activate
or clear the system-wide stop. Keep this setting in the backend deployment's
secret/environment configuration, not in frontend code. This is an environment-
configured administrator allowlist, not a full role-management system.


## Browser origin security

Cross-origin browser requests are disabled unless the backend is configured with
`TAIMOBOT_CORS_ORIGINS`, a comma-separated list of exact trusted origins, for
example:

```text
TAIMOBOT_CORS_ORIGINS=https://dashboard.example.com
```

Only explicit HTTP(S) origins are accepted; wildcard origins, URL paths, query
strings and fragments are rejected at startup. Bearer-token access does not use
cookies, so cross-origin credentials are disabled. Leave the variable unset for
same-origin deployments or local Vite proxy development.


## Emergency-stop persistence

The global emergency-stop flag is stored in the backend SQLite database, not only
in process memory. It therefore remains active across API process restarts and is
shared by backend workers using the same database. If the singleton safety-state
record is unexpectedly missing, the API treats the state as stopped until an
authorized administrator repairs or resumes it.


## Strategy knowledge repository

The authenticated `GET /strategies` endpoint returns built-in reference techniques
and the signed-in user's own research notes. The initial reference library includes
scalping, day trading, swing trading, trend following, breakout, mean reversion,
price action, support and resistance, momentum, multi-timeframe analysis,
volatility-aware and session-based techniques.

Users can add research notes through `POST /strategies`. User contributions are
private to their owner. Every reference and new contribution is marked
`UNVALIDATED`: storing a technique does not mean it is profitable, tested, approved
or enabled for execution. The repository is the first foundation for later
backtesting, strategy comparison, strategy synthesis and explainable AI. Those
engines are not yet implemented.


## Operator runbook: abandoned restore-lock marker

This procedure is only for an abandoned restore-lock marker. It does **not**
restore or replace the live database, remove SQLite WAL/SHM sidecars, or change
trading mode. Keep the system in paper mode.

**Do not run this while any TaimoBot backend worker may still be active.**

1. Stop the backend service and all workers using this database. Confirm the
   process recorded in the marker is no longer running and that no maintenance
   or restore operation is in progress.
2. Preserve a copy of the live database and any adjacent `-wal` / `-shm`
   files for investigation. Do not delete, rename, or checkpoint those files
   as part of lock recovery.
   Preserve the adjacent `.restore.guard` file as well. It is the stable OS-lock
   coordination file shared by backend processes; do not delete or replace it
   during abandoned-marker recovery. The recovery helper removes only the
   `.restore.lock` marker after acquiring the guard exclusively.
3. From the `backend` directory, inspect the marker:

   ```bash
   python -c "from app.maintenance import inspect_restore_lock; print(inspect_restore_lock())"
   ```

4. Proceed only if the status is exactly
   `ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW`. Stop and escalate if
   the status is active, unreadable, unsafe, or changed during inspection.
5. Run the explicit operator-confirmed recovery below. Use a real operator
   identity and an incident/ticket reference:

   ```bash
   python - <<'PY'
   from app.maintenance import inspect_restore_lock, recover_abandoned_restore_lock

   state = inspect_restore_lock()
   print("Current restore-lock state:", state)
   if state.get("status") != "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW":
       raise SystemExit("Refusing recovery: marker is not confirmed abandoned.")

   operator = input("Operator identity: ").strip()
   incident = input("Incident reference: ").strip()
   confirmation = input('Type RECOVER to remove only the abandoned marker: ')
   result = recover_abandoned_restore_lock(
       operator,
       incident,
       confirm=(confirmation == "RECOVER"),
   )
   print("Recovery result:", result)
   PY
   ```

6. Verify the returned status is `ABANDONED_RESTORE_LOCK_RECOVERED` and inspect
   the durable `restore-lock-recovery.audit.jsonl` file beside the database.
   Confirm both STARTED and COMPLETE events exist before restarting the service.

If the audit contains STARTED but no COMPLETE event, treat the operation as
**incomplete and requiring manual review**. The marker may already have been
removed; do not rerun recovery blindly. Reconcile the marker state, audit record,
database and sidecars first. If the audit STARTED write fails, the marker is
left in place. Never remove an active lock or an unreadable/unsafe marker by
hand as a shortcut.

This recovery helper is an out-of-band maintenance primitive, not an HTTP
endpoint. It requires filesystem access to the backend host. Actual database
restore execution, atomic database replacement and rollback remain separate,
unimplemented operations requiring their own approved procedure.
