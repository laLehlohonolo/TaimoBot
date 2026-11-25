# TaimoBot Reliability and Maintenance Hardening Plan

Status: engineering plan only. No application code has been changed by this document.
Base reviewed: `8dbe564afa3bad10f1fc77633a61ff3ec531afed`
Branch: `hardening/maintenance-worker-lifecycle`

## Objective

Make TaimoBot reliable, secure, recoverable, observable and testable across all seven layers. Improvements must preserve paper-trading-only operation, the backend-enforced 1% combined open-risk ceiling, and all existing account isolation and safety controls.

## Priority 0 — Maintenance worker lifecycle and backup safety

### Renewable leases and fencing
- Give each worker claim a monotonically increasing fencing token / generation, not only an expiry timestamp.
- Add heartbeat renewal with bounded renewal intervals and explicit lease ownership verification.
- Before every consequential side effect, verify the current claim and fencing token in the same transaction or guarded operation where possible.
- A superseded worker must be unable to publish, delete, mark complete, or otherwise mutate protected state.
- Make lease loss a terminal cancellation signal; workers must stop safely and release only resources they still own.

### Backup restore-readiness
- Define one shared restore-readiness validator used by backup creation, inventory, restore preflight and retention eligibility.
- Require SQLite integrity check, foreign-key check, required schema/table/column/index/version checks, and application-level invariants.
- A backup that fails any required check must never qualify for retention protection or replacement of an older backup.
- Preserve explicit provenance and validation timestamps; never infer unknown historical costs or data as zero.

### Durable publication and crash reconciliation
- Publish through a same-filesystem temporary file, flush and fsync the file, atomically replace the final path, then fsync the parent directory where supported.
- Record backup metadata only after the artifact is durably published and verified.
- Reconcile orphaned files, missing files, incomplete temporary files and metadata/artifact mismatches without destructive guessing.
- Retention must be conservative: retain uncertain artifacts until an auditable operator decision resolves them.

### Required fault and concurrency tests
- Concurrent scheduler ticks and duplicate claims.
- Lease expiry while the original worker is still running.
- Heartbeat renewal, renewal failure and stale fencing token.
- Superseded worker attempts cleanup/deletion.
- Process death before publish, after publish and before metadata commit.
- fsync/rename failures and partial writes.
- Cleanup racing with a newly created backup.
- Corrupt database, missing schema object, foreign-key violation and restore-readiness mismatch.
- Repeated recovery/reconciliation is idempotent and does not delete the only known-good backup.

## Priority 1 — Core trading safety and accounting
- Verify transactional order lifecycle and idempotency at every state transition.
- Validate stop-loss presence, direction, distance, instrument precision and broker constraints before sizing or execution.
- Recompute position size server-side using verified contract specifications, account equity, currency conversion and costs.
- Enforce per-trade and aggregate open-risk limits server-side; no frontend override.
- Verify emergency stop blocks new exposure and defines safe handling of existing paper positions.
- Reconcile paper balances, realized/unrealized P&L, spread, commission, swap and other costs.
- Keep demo ledger and any future real-money wallet strictly separate; real-money functionality remains disabled until all legal, regulatory, safeguarding, KYC/AML/FICA, payment, privacy and operational gates pass.

## Priority 2 — Market data and strategy research readiness
- Do not enable backtesting until market data provenance, continuity, timestamps, instrument specification, cost assumptions and deterministic replay are verified.
- Preserve explicit gaps and unverified data states; never promote incomplete or empty data to verified coverage.
- Keep XAUUSD execution disabled until a real supported feed and verified broker/instrument specifications are configured.
- Strategy learning must be session-aware and evidence-led; candidate changes require versioning, reproducibility, out-of-sample validation and paper observation.
- No strategy may alter the 1% risk ceiling or autonomously enable live trading.

## Priority 3 — Taimo Brain administrator intelligence
- Implement the agreed admin-only daily reports at 05:30, 08:30, 11:00, 15:00, 18:00 and 23:30 Africa/Johannesburg time, with daylight-saving-aware session calendars.
- Provide immediate prioritized alerts for material events.
- Keep participant net trading performance, actual platform revenue and combined ecosystem metric separately visible and never double-count.
- Forecasts must be ranges with assumptions, confidence, coverage and uncertainty; no guaranteed earnings.
- Never expose administrator intelligence or internal discussions to participants.

## Priority 4 — Operational quality
- Add structured logs, metrics, tracing/correlation IDs, health/readiness checks and actionable alerts.
- Define backup/restore runbooks, incident response, rollback criteria and recovery-time/recovery-point objectives.
- Test migrations from realistic prior database versions with failure injection and restart recovery.
- Add security checks for authentication, authorization, secrets, rate limits, input validation, audit trails and dependency vulnerabilities.
- Ensure CI exercises critical invariants and integration paths; tests must be repeatable and report exact coverage/results.

## Release gates

A change is not considered complete because code was written or a dashboard renders. It must:
1. Have a reviewed implementation and threat/risk assessment.
2. Pass focused regression, concurrency, fault-injection and integration tests.
3. Preserve all safety invariants.
4. Produce auditable evidence and clear operational instructions.
5. Be reviewed before merge; deployment requires a separate explicit approval.

## Current limitations

This document is a plan. It does not implement any of the above controls, run tests, merge branches, or deploy software. The reviewed development CI results are historical results for their recorded commit and do not prove these proposed hardening changes.
