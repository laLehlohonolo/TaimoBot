# Seven-Layer Implementation and Interface Readiness Matrix

**Baseline reviewed:** PR #4 head `5d88012d48c7fe8db77f54918ec50cedb06e9689`  
**Purpose:** distinguish implemented capabilities from architectural intent before expanding the participant, administrator, and Taimo Brain interfaces.

This is an implementation-readiness record, not a claim that the complete seven-layer platform is already operational.

## 1. Layer readiness

| Layer | Evidence in the current repository | Readiness | Required next integration |
|---|---|---|---|
| Market | Instrument catalogue, Deriv public market-data client, observation storage/quality, and admin backfill/sync routes. Provider coverage and broker specifications are not universal; XAUUSD feed/specifications are not verified. | Partial; observation-first | Per-instrument provider, freshness, coverage, gap and specification status. Keep unverified instruments observation-only. |
| Analysis | Market observations and quality inspection provide inputs, but a complete, versioned multi-timeframe analysis service and explainable feature/regime pipeline are not evidenced as integrated. | Not yet established as a complete layer | Define reproducible analysis snapshots, source timestamps, feature versions, uncertainty and data-quality gates. |
| Strategy | Built-in reference techniques and participant strategy notes exist; entries remain unvalidated. Strategy Lab routes exist, but a validated candidate/backtest/release lifecycle is not established by the current frontend. | Partial foundation | Separate reference knowledge, hypotheses, experiments, validated candidates and approved strategies. Never promote a note to an executable signal. |
| Execution | Paper account/order lifecycle, duplicate-order protection, close accounting, copy-trading and group lifecycle are implemented and regression-tested. No live broker execution. | Paper-only foundation | Add explicit execution-state telemetry and idempotent event contracts. Preserve paper-only mode and authorization checks. |
| Risk | Deterministic backend risk calculation and account open-risk aggregation are implemented; the enforced maximum remains 1%. | Core safeguard present | Expose read-only risk decisions, reserved/open risk, rejection reasons and reconciliation state. No UI or Brain override. |
| Learning | Historical observations, strategy knowledge and outcome records exist, but a durable experiment registry, leakage-safe evaluation and approved model lifecycle are not established as a complete service. | Not yet established | Build experiment provenance, baseline/candidate comparison, walk-forward evaluation, paper/shadow evidence and approval records. |
| Legacy | Maintenance scheduler, backups, recovery inventory, restore preparation/approval/preflight and audit-oriented recovery records exist. | Partial operational foundation | Unify durable architecture decisions, incident records, release history, audit references and rollback knowledge without weakening recovery controls. |

## 2. Interface readiness and data boundaries

### Participant Dashboard
- Own accounts, settlement currency, paper balances, open risk, positions and personal trade history only.
- Clearly label all balances and results as paper/virtual.
- Read-only trade execution in the current frontend; do not display a working order-placement control until the complete safe user flow is implemented and tested.
- Never expose other participants, administrator-only reports, internal strategy parameters or Brain research.

### Administrator Dashboard
- Server-side administrator authorization on every protected operation; frontend visibility is not authorization.
- Aggregate participant/platform metrics only from verified, permission-scoped records.
- Show operational states with timestamps and explicit unknown/degraded states.
- Keep participant net paper performance separate from actual platform revenue. Do not imply paper gains are company revenue.
- Destructive, financial, identity, risk and release actions require explicit authorization, confirmation, audit records and appropriate independent gates.

### Taimo Brain Intelligence Office
- Administrator-only, separately provisioned access with MFA as a launch requirement.
- Show seven-layer telemetry only when a layer publishes a real, timestamped health contract.
- Every insight must include source, timestamp, scope, confidence/uncertainty and data limitations.
- Brain may observe, research, compare, test in isolation and recommend. It cannot approve itself, alter deterministic risk controls, enable live execution, move funds or deploy production changes.

## 3. Minimum shared layer-health contract

Each layer should publish a read-only status record with these fields:

- `layer_id`: one of MARKET, ANALYSIS, STRATEGY, EXECUTION, RISK, LEARNING, LEGACY.
- `status`: HEALTHY, DEGRADED, OFFLINE, BLOCKED, or NOT_IMPLEMENTED.
- `observed_at`: timezone-aware timestamp.
- `last_success_at`: timestamp or null.
- `source_version`: service/build or data-contract version.
- `dependencies`: named dependencies and their status.
- `capabilities`: explicitly available operations, not aspirational features.
- `limitations`: current gaps, stale data, disabled instruments or missing prerequisites.
- `evidence_ref`: traceable event, check, job or audit reference where available.

A missing heartbeat must not be translated into HEALTHY. A missing field must not be fabricated. The UI should render NOT_IMPLEMENTED or UNKNOWN until an authenticated service supplies evidence.

## 4. Delivery order and acceptance gates

1. **Contracts first:** define typed event, health, identity-scope and audit contracts. Add schema validation and contract tests.
2. **Participant dashboard:** connect only existing account-scoped endpoints; test ownership isolation, empty states, stale responses, currency labels and balance/risk reconciliation.
3. **Administrator operations:** add a protected backend read model before building aggregate views. Test participant-data boundaries and every privileged route.
4. **Seven-layer telemetry:** integrate one layer at a time. Each status must be sourced from real checks and include freshness/dependency information.
5. **Taimo Brain foundation:** implement supervised worker health, durable event intake, checkpoints, bounded queues and restart recovery before presenting a live Brain status.
6. **Learning and research:** require reproducible data provenance, no-look-ahead evaluation, baseline comparisons and explicit administrator approval.
7. **Release governance:** CI, security review, migration checks, staged paper/shadow evidence, separate production confirmation and tested rollback.

## 5. Non-negotiable safety invariants

- Paper trading only; no live broker orders or real-money movement.
- Backend-enforced maximum 1% combined open risk per account remains authoritative.
- XAUUSD stays execution-disabled until its market feed and broker specifications are verified.
- Other instruments remain observation-only until independently verified and approved.
- No unvalidated strategy can become an executable signal.
- Administrator and Brain data never leak into participant scope.
- A green CI run proves only the checks that actually ran; it does not prove the full platform is production-ready.

## 6. Current checkpoint

The latest recorded CI run for the reviewed head passed its frontend and backend jobs, including the production frontend build, isolated frontend-to-backend HTTP integration, full backend test suite, FastAPI verification and health check. This is a verified regression checkpoint, not acceptance of the remaining architecture stages.

PR #4 remains draft, open and unmerged. No deployment or live trading is authorized by this document.
