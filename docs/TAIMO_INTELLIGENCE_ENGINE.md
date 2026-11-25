# TaimoBot Market Intelligence Engine

## Product requirement

TaimoBot must continuously improve its market understanding from verified market evidence. Any new market information that materially affects an instrument, market regime, volatility, liquidity, trend, session, cost assumption, signal quality, or strategy evaluation must be ingested, validated, stored, assessed, and reflected in the next eligible intelligence update.

The dedicated daily research, refresh, learning, and next-session preparation window is **23:00–00:00 Africa/Johannesburg (SAST)**. This is a daily operating window, not a claim that every possible research task can always finish in sixty minutes. Work must be bounded, resumable, checkpointed, and prioritised.

## Historical market archive

For every enabled observation instrument, TaimoBot should:

1. Discover and verify the provider's current symbol identity and metadata.
2. Determine the earliest historical timestamp the provider actually makes available for that exact instrument and data product.
3. Backfill OHLC candles from that earliest available timestamp through the present, in bounded, provider-compliant time chunks. Where useful and available, separately collect tick history for microstructure research.
4. Store the provider, symbol mapping, timeframe/granularity, UTC event timestamp, retrieval timestamp, raw/normalized provenance, request range, response coverage, and any provider limitation.
5. Use durable per-symbol/per-timeframe checkpoints and idempotent inserts so interrupted backfills can resume without duplicate observations.
6. Detect missing, duplicated, out-of-order, stale, malformed, or timestamp-misaligned observations. Retry bounded gaps and retain an explicit unresolved-gap record instead of silently filling or fabricating data.
7. Report historical coverage honestly: earliest confirmed candle, latest confirmed candle, available period, gaps, source, and last successful sync.

"From the day the market started" means **from the provider's earliest verifiably available history for that instrument**. Provider retention, symbol launches, licensing, API limits, and endpoint changes may prevent true inception-to-present coverage. TaimoBot must never invent missing candles or claim full inception history without evidence.

The existing Deriv public client is a read-only foundation. Its current candle request is capped at 500 candles per request and currently fetches the latest window. It must be extended through bounded historical start/end pagination and resumable checkpoints before it can satisfy the full-history requirement. Deriv history requests must remain unauthenticated/read-only and must not use proposal, buy, sell, or other execution APIs.

## Daily 23:00–00:00 intelligence cycle

The daily cycle runs in the IANA timezone `Africa/Johannesburg`, with a single durable run identity per local calendar date. It must be safe across restarts, multiple workers, daylight/timezone changes, and duplicate scheduler triggers.

### 23:00 — collect and reconcile

- Fetch the incremental history since each instrument's last confirmed checkpoint.
- Repair bounded historical gaps and continue any unfinished backfill.
- Refresh active-symbol/provider metadata and verify symbol availability.
- Validate timestamps, OHLC relationships, duplicates, ordering, freshness, and source provenance.
- Record data quality, coverage, and API failures; never promote unverified data to trusted research evidence.

### 23:00–23:30 — research and analyse

- Compare current price action with the instrument's historical baseline.
- Analyse multi-timeframe trend, structure, ranges, volatility, momentum, reversals, breakouts, drawdowns, and recurring chart behaviour.
- Study session behaviour, spreads/costs and liquidity where trustworthy source data exists.
- For Forex and XAUUSD, distinguish session and macroeconomic context from price-only evidence. For Deriv synthetic indices, analyse their own continuous synthetic-market behaviour; do not apply Forex sessions or fundamentals as if they were causal.
- Evaluate signal and paper-trade outcomes, including net results after spread, commission, swap, slippage assumptions, and other applicable costs.
- Identify changed regimes, degraded strategies, false positives, data anomalies, and uncertainty.

### 23:30–23:50 — learn and challenge

- Update descriptive statistics, feature sets, regime labels, and evidence-backed research notes.
- Compare candidate model/strategy revisions against a frozen baseline using chronological out-of-sample and walk-forward evaluation. Prevent look-ahead leakage and survivorship bias.
- Use minimum sample-size and market-regime coverage gates. Mark thin evidence as inconclusive.
- Keep a versioned Taimo Intelligence Journal: observation, source, sample window, hypothesis, test, baseline, candidate, metrics, limitations, decision, and rollback reference.
- A candidate that does not beat the baseline after costs and risk-adjusted evaluation is rejected or kept experimental. No self-reported "learning" without a reproducible record.

### 23:50–00:00 — prepare and publish

- Publish a timestamped next-session research brief: market regime, key levels/areas where supported, volatility context, relevant calendar/context, candidate setups, invalidation conditions, data freshness, confidence, and uncertainty.
- Publish model/strategy version and evaluation status; distinguish approved research from experimental candidates.
- Save a run summary and durable checkpoints. If tasks remain, queue them for resumable continuation rather than skipping quality checks.
- Keep the system in its prior safe state if data is stale, incomplete, contradictory, or a candidate fails validation.

## Intelligence and execution boundaries

- The intelligence engine may observe, research, rank hypotheses, evaluate, and recommend. It must not autonomously raise risk, alter hard safety limits, activate new instruments for execution, disable emergency stops, or bypass Strategy, Risk, or Execution approvals.
- The hard maximum remains 1% risk per trade, with account-wide exposure and correlation controls. Risk decisions are enforced by the backend.
- XAUUSD remains the only execution-enabled instrument under the current project scope. Additional FX pairs and Deriv synthetic indices remain observation-only until instrument-specific provider/broker specifications, conversion, costs, and risk controls are independently verified and explicitly approved.
- Paper trading remains the only permitted trading mode. No live orders or real-money wallet actions are introduced by this engine.
- A model update is not automatically a strategy deployment. Candidate versions must pass deterministic tests, historical walk-forward evaluation, paper shadow/challenger evaluation, and explicit release approval.
- Maintain reproducibility: version data snapshots, feature definitions, model artifacts, parameters, code revision, evaluation windows, and decision records. Support rollback to the last approved version.

## Scheduling and operational reliability

- Use a durable scheduler/worker, not a request handler or a process-local timer that disappears on restart.
- Trigger at 23:00 SAST daily. Acquire a database-backed/OS-safe single-run lease; duplicate triggers must not run the same date's cycle twice.
- Persist stage status, attempt count, checkpoints, started/completed timestamps, and failure reasons. Make each stage idempotent and resumable.
- Set provider rate limits, bounded concurrency, retry/backoff, request timeouts, and a strict 00:00 SAST hand-off deadline for the preparation brief. Any unfinished historical backfill continues incrementally on later cycles.
- The daily intelligence cycle must coordinate with backup/restore maintenance and never interfere with restore locks, database consistency, emergency stop, or paper-trade transaction safety.
- Alert on missing cycles, stale feeds, unresolved gaps, provider schema changes, failed evaluation, and incomplete next-session briefs.

## Current implementation status

The repository currently has validated market-observation normalization/storage, quality diagnostics, a public read-only Deriv symbol discovery/candle client, and a SAST 23:00–00:00 maintenance-window predicate. The Deriv client currently requests up to 500 latest candles per request; it does not yet implement earliest-available historical backfill, a durable per-instrument backfill cursor, or a complete scheduled intelligence/research/learning cycle.

This document records the required target behaviour. It does not claim that the full engine or scheduler is already implemented, nor that historical data has been downloaded. Implementation must be delivered in tested, incremental changes with the safety boundaries above preserved.

## Administrator-only intelligence office and controlled improvements

Taimo Intelligence must have two deliberately separate information surfaces.

### Private administrator intelligence office

Only explicitly provisioned administrator roles with MFA and server-side authorization may access the full intelligence and engineering record. This is an internal command centre, not a participant dashboard. It must include:

- A unified feed of market research, source updates, historical-data coverage, data-quality warnings, model observations, strategy evaluations, rejected hypotheses, and daily-cycle completion.
- A layer-by-layer health view for Market, Analysis, Strategy, Execution, Risk, Learning, and Legacy, including service status, version, dependencies, freshness, error rates, and recent changes.
- Security and operational alerts: suspicious access, authorization failures, unusual API activity, dependency vulnerabilities, malware/antivirus detections where supported by actual security tooling, integrity failures, attempted policy bypasses, scheduler failures, and resource/availability incidents. Do not claim antivirus or threat detection exists unless a scanner/monitor is integrated and reporting verified results.
- Strategy and model comparison: approved baseline versus candidate, test window, sample size, market regimes, net-of-cost results, drawdown, risk-adjusted measures, rejection reasons, uncertainty, and rollback target.
- Participant-level and aggregate operational analytics, with privacy-scoped access and a complete audit trail for cross-participant inspection.
- Time filters for daily, weekly, monthly, yearly and custom periods; segmentation by individual participant, Standard/Premium tier, instrument, strategy version, and cohort where sample size/privacy permits.
- Distinct ledgers and clearly labelled units: paper-trading balances/P&L, realised paper profit-taking, unrealised paper P&L, fees/cost assumptions, and—only if legally launched—real-money deposits, withdrawals, fees and settled balances. Never add virtual paper amounts to real money or combine currencies without a disclosed, timestamped conversion method.
- Historical charts and downloadable reports with source, scope, timezone, currency, calculation definition and generated-at timestamp.

The AI may explain findings and propose changes, but it must not grant itself administrator privileges, hide adverse results, rewrite audit history, approve its own proposals, deploy code, change execution permissions, or move funds.

### Participant-facing information boundary

Participants receive only information needed to understand and safely use their own account:

- Their own account status, permitted features, paper-trading balance and trade history.
- A concise, factual explanation of why a trade was entered or rejected, without exposing private strategy parameters, source code, other participants' records, internal model reasoning, or exploitable security details.
- Their own realised/unrealised paper results, with explicit virtual/non-withdrawable labels and clear cost assumptions.
- Relevant market-calendar notices and risk warnings, such as an upcoming CPI release, with event time in the participant's selected/local display timezone and a source timestamp. Avoid implying that an event guarantees a price move.
- Platform maintenance, service status, and approved participant announcements.
- General financial-literacy material that is educational, non-personalised where appropriate, and never promises returns or directs a participant to risk money they cannot afford to lose.

A participant notification feed must be account-scoped. It must never expose internal intelligence reports, model upgrades, experimental strategies, security investigations, administrator notes, other users' data, or product keys.

### Admin-controlled "Approve and Update" release workflow

The administrator must have a review queue for intelligence-generated improvement proposals. Each proposal includes its problem statement, evidence/source links, affected layers and files, expected benefit, risk assessment, test results, sample size, limitations, compatibility impact, migration/rollback plan, and proposed version.

The workflow is:

1. Intelligence creates a versioned proposal; it cannot change production code or approved model settings.
2. Automated checks run in an isolated environment: unit/integration/security tests, reproducible historical walk-forward evaluation, leakage checks, paper/shadow comparison, and regression checks against the current approved baseline.
3. The administrator reviews the evidence and chooses APPROVE FOR STAGING, REJECT, or REQUEST REVISION. Approval requires MFA/step-up confirmation and a recorded reason.
4. Approval authorises a controlled staging/release candidate, not an unconditional production deployment. CI, security checks, migration checks, readiness checks and paper/shadow gates must still pass.
5. A separate explicit production-release confirmation is required after staging evidence is available. High-impact changes (Risk, Execution, authentication, money/ledger, permissions, or security controls) require enhanced review and must never be self-approved by the proposing AI.
6. Release is versioned, observable, reversible and canary/gradual where feasible. Automatic rollback triggers must be defined for failed health checks, degraded paper performance, data integrity issues or safety regressions.
7. Record proposer, approver, code/model/data versions, test evidence, release actor, timestamps, affected components, outcome and rollback reference in an append-only audit record.

The button label may read "Approve and Update", but its confirmation screen must state exactly what will change and whether the action is staging approval or production release. It must never mean "skip tests and immediately rewrite live code." The AI must not edit its own release controls or the 1% risk ceiling.

### Daily reporting and operational definitions

The daily intelligence report must separate:
- New information collected and its verified source/freshness.
- Trades taken, trades rejected, and the counterfactual evaluation of rejected opportunities where reliable data exists.
- Profits taken and exits, measured net of costs where available.
- Losses, drawdown, and preventability analysis without implying all losses can be eliminated.
- Strategy/model candidates accepted, rejected, inconclusive or awaiting approval.
- Data gaps, outages, security alerts, unresolved risks and work deferred to the next cycle.

Every metric must define its population, time basis, currency, realised/unrealised status, and whether it is demo/paper or real money. Do not present win rate alone as proof of profitability. No AI message may promise that participants will make money or that losses will never occur.
