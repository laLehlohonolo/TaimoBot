# Taimo Brain Admin Console — Implementation Specification

Status: specification only; implementation and security tests still required.
Target branch: feature/taimo-brain-admin-console-spec
Baseline: 8dbe564afa3bad10f1fc77633a61ff3ec531afed

## 1. Purpose and visibility

Provide a private, administrator-only workspace for the platform owner to communicate with Taimo Brain and receive operational intelligence. This workspace is not part of the participant experience.

The backend is the security boundary. Hiding a navigation item in React is not authorization. Every endpoint, message, report, attachment, notification and export must require an authenticated, verified ADMIN role. Participant tokens must receive 403 responses. Never expose admin-console routes or data through participant profile, dashboard, public API responses, client bundles or participant notifications.

## 2. Admin console areas

### A. Two-way Taimo Brain conversation
- Admin can ask questions, request explanations, challenge recommendations and assign follow-up investigations.
- Taimo Brain can send a message when a material issue, regression, missing dependency or safety concern is detected.
- Persist conversation threads, timestamps, author (admin or Brain), delivery/read state, evidence references and audit events.
- Brain messages must distinguish verified facts, inferences, unknowns and recommendations.
- Brain may recommend changes or prepare a reviewed change proposal; it must not silently change risk rules, execute trades, alter participant balances, change subscription entitlements, deploy code or merge PRs.

### B. System completeness and engineering backlog
Show what is complete, incomplete, blocked, unverified and overdue. Each finding includes affected layer/component, severity, evidence, owner, recommended action, dependencies and verification test. Never label documentation as an implemented capability.

### C. Operational performance reporting
Generate:
- Daily: availability, API latency/error rates, failed jobs, order lifecycle outcomes, risk blocks, data freshness, incidents and unresolved critical alerts.
- Weekly: trends, recurring failures, regression changes, security events, delivery progress and next priorities.
- Monthly: reliability, performance against service objectives, risk-control effectiveness, data quality, recovery readiness and technical debt.
- Annual: architecture health, security posture, incident patterns, strategy/research maturity, capacity, compliance readiness and investment priorities.

Every report must show its reporting window, timezone (Africa/Johannesburg), data coverage, calculation definitions and missing telemetry. Do not fabricate metrics when instrumentation is absent.

### D. Security and bypass monitoring
Detect and report evidence-backed anomalies such as repeated authentication failures, rate-limit abuse, privilege escalation attempts, account-ownership mismatches, suspicious session patterns, duplicate/idempotency conflicts, unusual API access and attempted risk-limit circumvention.

Use neutral language such as “suspected bypass attempt” or “anomalous activity.” Show event time, affected endpoint/account identifier (minimize personal data), detection rule, evidence, confidence, action taken and recommended investigation. Do not claim to know who is responsible from an IP address or anomaly alone. Preserve privacy, retention limits and an immutable security audit trail. Critical alerts must be visible to the admin without being exposed to participants.

### E. Recommendations and upgrade planning
Taimo Brain ranks improvements by severity, user impact, risk reduction, effort, dependencies and evidence confidence. Present two clear paths where appropriate: (1) minimum safe remediation and (2) stronger/longer-term improvement. Admin approval is required before changes are applied. Include rollback and acceptance tests.



### F. Knowledge and Innovation Intelligence
- Maintain a permanent Knowledge & Innovation Intelligence capability for Taimo Brain.
- Monitor relevant, credible developments in trading research, market structure, quantitative methods, software engineering, AI/ML, cybersecurity, data infrastructure and applicable platform/security standards.
- Prioritize items by relevance to TaimoBot, evidence quality, practical impact, urgency and implementation risk. Provide source links, publication dates, a concise explanation, confidence/limitations and a recommended action.
- Send urgent, actionable security or operational intelligence as prioritized admin alerts. Curated non-urgent findings may be included in the weekly admin intelligence digest. Never send internal admin intelligence to participants.
- Distinguish verified findings from hypotheses, vendor claims, speculation and untested recommendations. Treat retrieved content and external data as untrusted input.

### G. Continuous Strategy Research and Session-by-Session Improvement
- Strategy research is a permanent capability, not a one-off seven-day task. The initial strategy-development challenge remains seven days once trustworthy data, cost assumptions and deterministic replay/backtest infrastructure are validated.
- After every completed trading session and each trading day, evaluate strategy behaviour and identify evidence-backed improvements. Cover the Asian, London and New York sessions and their overlaps; define session boundaries in Africa/Johannesburg time using daylight-saving-aware market calendars rather than hard-coded year-round UTC offsets.
- Assess session-specific market regimes, volatility, liquidity proxies, spread and slippage, costs, signal quality, entries/exits, missed opportunities, drawdowns, risk utilisation, execution outcomes and paper-trading P&L where telemetry supports them.
- Compare results against versioned baselines and prior sessions. Record what changed, why it changed, evidence, uncertainty, expected benefit, possible failure modes and the tests required.
- Generate and test controlled strategy hypotheses. Preserve independent strategy candidates; do not combine candidates until each has been independently evaluated and compatibility is demonstrated.
- Every candidate or change must be reproducible and versioned, with data provenance, parameter/configuration snapshot, code/version reference, experiment logs and evaluation results.
- Validation gates include data-quality and coverage checks, realistic spread/commission/swap/slippage assumptions, out-of-sample and walk-forward evaluation where appropriate, drawdown and exposure analysis, robustness/sensitivity checks, and paper-trading observation.
- Do not optimize solely for headline returns or overfit individual sessions. Report sample size, uncertainty, regime dependence and negative results. A session with insufficient or invalid data must be marked inconclusive, not treated as evidence of improvement.
- Continuous learning may recommend and test changes in a research/paper environment, but it must not silently promote a strategy, change the 1% risk ceiling, bypass backend risk controls, or enable live trading. Admin approval and release gates remain mandatory.
- Long-term research ambition remains approximately two or three genuinely distinct strategy candidates per year, without forcing a quota or lowering validation standards.


### H. Daily Potential, Financial Outlook and Participant Allocation Intelligence
- Provide a private administrator-only daily outlook, refreshed before the trading day and updated as material market conditions change. This is a forecast and planning aid, never a promise of income or a guaranteed daily target.
- Summarize the day's operating plan: current system readiness, market regime, relevant financial/economic headlines and scheduled events, likely opportunity areas, risks, and the Asian, London and New York session windows plus overlaps. Use dated, sourced information and Africa/Johannesburg timestamps; account for daylight-saving changes in market calendars.
- For each potential opportunity window, state the instrument, thesis, conditions required, invalidation conditions, expected holding/time window if estimable, spread/liquidity considerations, risk constraints, confidence level, evidence and uncertainty. If the evidence is insufficient, say “no reliable forecast” or “no trade”; do not manufacture a time or opportunity.
- Produce a platform-wide scenario outlook and participant-level estimates only from verified inputs. Inputs may include eligible participant count, each participant's actual available trading capital, subscription tier and entitlements, approved allocation/profit-sharing rules, permitted instruments, current exposure, costs, verified broker specifications, data quality, and validated strategy performance. Keep each participant's account, balance, P&L, currency, risk, positions and history isolated.
- Show conservative/base/upside scenarios as ranges, with assumptions and confidence—not a single certain figure. Clearly separate (a) platform revenue from subscriptions/fees, (b) trading P&L, (c) participant net outcomes, and (d) any platform share or distribution. Never add unlike amounts or currencies without verified conversion rates and explicit labels.
- A proposed platform ambition such as R1,000,000 per day may be displayed only as an administrator-set aspirational target. It must never be presented as an achievable forecast unless independently supported by validated capacity, capital, participant activity, strategy evidence, costs and lawful commercial terms. Show the gap between ambition and evidence-based scenarios.
- For each subscription tier, calculate only the entitlements and limits actually configured and legally approved. Show per-participant and aggregate estimates, explain the allocation formula, and flag missing or conflicting subscription/profit-limit rules rather than inventing percentages or payouts.
- Never imply that a participant will earn a fixed daily or weekly amount. Trading returns vary; losses, flat days and no-trade sessions are valid outcomes. Display downside scenarios and the possibility of zero or negative returns alongside upside.
- Forecasts must incorporate the backend-enforced maximum 1% risk per trade, aggregate open exposure, margin, drawdown, transaction costs (spread, commission, swap and slippage), account currency and verified contract specifications. Do not treat the 1% risk ceiling as a profit target.
- Compare forecasts with realized outcomes after each session and day. Record forecast version, timestamp, input provenance, assumptions, confidence, actual result, forecast error and explanation. Recalibrate only through tested, versioned methods; do not silently change strategy parameters or risk controls.
- Use clear labels: “aspirational target,” “scenario estimate,” “forecast,” “realized result,” “unknown/unavailable.” Show data coverage and stale/missing inputs prominently. If inputs fail validation, suppress numeric forecasts and explain why.
- This intelligence is administrator-only. No forecast, headline, internal target, participant comparison or strategy assessment may leak into participant pages, APIs, notifications or exports. Participant-facing financial information must be limited to that participant's own verified account records and approved disclosures.


## 3. Participant profile boundary

Participants receive their own profile area only. It should support a participant-chosen display/profile name, with clear validation and uniqueness rules where needed, plus only the account/profile fields the product explicitly requires. A participant can read and update only their own permitted fields. Admin access to participant records must be separately authorized and audited. Never expose private Brain conversations, internal performance reports, security investigations or other participants' profile data.

## 4. Backend data and API requirements

Proposed resources (final names must follow existing project conventions):
- admin_brain_threads and admin_brain_messages
- admin_brain_notifications
- system_findings and remediation_proposals
- performance_report_runs and performance_report_metrics
- security_events and detection_rules
- participant_profile fields / profile audit events

Requirements:
- Enforce ADMIN authorization server-side on every admin route.
- Scope every query by authorized role and resource; deny by default.
- Validate and rate-limit message creation; protect against prompt injection in logs, user content and retrieved data.
- Keep Brain outputs advisory. Tool/action execution requires explicit admin approval and a separate allowlist.
- Store secrets outside source control; encrypt sensitive data at rest and in transit.
- Record who viewed, created, approved, exported or changed sensitive admin records.
- Add pagination, retention controls, redaction and safe export rules.
- Make scheduled report generation idempotent and timezone-aware.
- Fail closed if role, report coverage, event provenance or authorization cannot be verified.

## 5. Acceptance tests

1. Participant session cannot load admin pages or call any admin endpoint (403); verify across every route, not only UI navigation.
2. Non-admin cannot read, create, search, export or receive admin messages/reports/security events.
3. Admin can send a message and receive a persisted Brain reply with evidence and confidence labels.
4. Brain can notify admin of a verified finding; delivery failure is observable and retry-safe.
5. Daily, weekly, monthly and annual reports use exact periods in Africa/Johannesburg and disclose missing data.
6. A missing feature is shown as missing/unverified, never falsely marked complete.
7. Security anomaly alerts include evidence and avoid unsupported identity attribution.
8. Brain cannot change risk limits, create/close orders, move funds, change subscriptions, merge or deploy without explicit separately authorized approval.
9. Participant A cannot read or edit Participant B's profile; participant profile changes are audited.
10. Admin-only content is absent from participant API payloads, browser routes, notifications and exports.

## 6. Delivery sequence

1. Inventory current authentication roles, admin routes, profile schema and audit infrastructure.
2. Complete the previously prioritized maintenance-worker hardening: renewable leases/heartbeats, ownership checks before every consequential side effect, safe takeover, and concurrent cleanup tests.
3. Implement backend admin authorization and persistence before the UI.
4. Implement the admin conversation and findings inbox.
5. Add evidence-backed daily/weekly/monthly/annual reports and alert delivery.
6. Add security anomaly monitoring with false-positive review and privacy controls.
7. Implement participant profile-name support with strict self-access tests.
8. Run backend, frontend, authorization, concurrency, failure-injection and integration suites; review the full diff before any merge.

## 7. Non-negotiable TaimoBot safety boundary

Paper trading only. XAUUSD remains the only execution-approved instrument, subject to verified feed/specifications. The backend-enforced maximum risk remains 1%. No live orders or real-money operations. Taimo Brain is an advisor and monitor, not an autonomous authority to change financial or security controls.
