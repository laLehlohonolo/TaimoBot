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
