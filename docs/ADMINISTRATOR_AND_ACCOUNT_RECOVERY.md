# TaimoBot Administrator, Withdrawal Approval, and Account Recovery

## Administrator identity and access

TaimoBot must support a dedicated administrator identity and login flow, separate from participant sign-in and participant dashboards. Administrator access is granted only to explicitly provisioned administrator accounts; public registration must never create an administrator. Role assignment must be server-side, least-privilege, auditable, and protected against participant self-escalation.

The administrator control centre must provide:

- A searchable participant directory with account status, join date, subscription tier (Premium or Standard), and linked participant dashboards.
- A controlled, read-only view of each participant's dashboard, clearly marked as an administrator view. Administrative inspection must not impersonate the participant or inherit participant permissions.
- Platform-wide operational insights: participant counts by tier, active/inactive accounts, paper-trading activity, instrument usage, signal and strategy performance, aggregate paper P&L, system health, data freshness, risk rejections, emergency-stop status, and intelligence-cycle outcomes.
- Aggregate financial/ledger reporting that distinguishes virtual paper balances and P&L from real-money balances, deposits, withdrawals, fees, and provider settlement. Never combine these into one total.
- Auditable administrative actions, including actor, target, action, timestamp, reason, before/after state where appropriate, and correlation ID.

The administrator must not be able to view plaintext passwords, password hashes, reset tokens, session tokens, payment credentials, or full sensitive identity documents through dashboard screens. Sensitive participant information must be minimised and permission-scoped. Every cross-participant inspection and export must be logged.

## Premium and Standard tiers

Every participant record must have one explicit subscription tier: STANDARD or PREMIUM. Tier entitlements must be centrally defined and enforced by the backend, not trusted from frontend labels. The administrator can view tier membership and aggregate performance by tier. Any future tier change must be an authenticated, audited administrative action with effective time and reason. Tier status must not change a participant's actual wallet currency, money ownership, risk limits, or trading ledger.

## Withdrawal approval policy

Real-money deposits and withdrawals remain disabled until all applicable legal/regulatory, payment-provider, KYC/AML/FICA, POPIA/privacy, safeguarding, security, reconciliation, and operational launch gates are independently satisfied.

When real-money withdrawals are eventually enabled:

1. A participant submits a withdrawal request against their verified real-money wallet only. The demo/paper ledger is never a funding source.
2. The first-ever withdrawal for each participant enters a mandatory PENDING_FIRST_WITHDRAWAL_ADMIN_APPROVAL state. It must not be sent to a payment provider or reduce available funds until an authorised administrator approves it.
3. The administrator sees the minimum necessary review details: participant identity-verification status, verified beneficiary/bank-account ownership status, request amount and currency, available real-money balance, relevant risk/compliance flags, and prior withdrawal history. Full bank credentials should be masked.
4. Approval or rejection requires explicit confirmation and a recorded reason. The decision is immutable in the audit trail. The requesting administrator cannot silently alter a completed decision.
5. Approval is only one gate. Compliance screening, available-funds reservation, beneficiary validation, velocity/limits checks, idempotency, provider acceptance, settlement confirmation, and reconciliation remain mandatory.
6. The withdrawal lifecycle must be explicit and idempotent: REQUESTED, PENDING_FIRST_WITHDRAWAL_ADMIN_APPROVAL, APPROVED, REJECTED, COMPLIANCE_REVIEW, FUNDS_RESERVED, SUBMITTED_TO_PROVIDER, PROCESSING, PAID, FAILED, CANCELLED, and REVERSED where applicable. Only valid transitions are permitted.
7. Rejected, failed, cancelled, or reversed withdrawals must have auditable ledger treatment. Never delete or overwrite a financial posting; use linked reversals.
8. Later withdrawals may follow a separately approved policy, but must still pass automated controls and any risk-based/manual review. The first-withdrawal approval requirement must not be bypassed by changing devices, bank details, or account state.

No real-money transfer, provider API call, withdrawal API, or cash balance is implemented by this requirements document. The existing paper-trading system remains paper-only.

## Password and account recovery

Participant recovery must be self-service and must not require an administrator to know or set a participant's password:

1. The participant submits their registered email address on the Forgot password screen.
2. The system returns the same neutral response whether or not the account exists, to prevent account enumeration.
3. For an eligible account, send a short-lived, single-use, cryptographically random reset link to the verified registered email address. Store only a secure hash of the reset token, with an expiry (target: 15 minutes), purpose, user ID, and consumed timestamp.
4. The reset page accepts a new password meeting the existing password policy and confirmation. Validate the token and consume it atomically so concurrent requests cannot reuse it.
5. On success, replace the password hash, revoke all existing sessions for that account, invalidate all outstanding reset tokens, and send a security notification to the registered email.
6. Rate-limit requests and token attempts, protect endpoints against abuse, use HTTPS, and never log tokens or passwords. Do not disclose whether an email is registered.
7. If email access is lost, provide a separate identity-verification support process. Administrators may initiate a documented recovery review but must never bypass identity verification or view/set the old password.

Administrator account recovery must be stricter than participant recovery: verified recovery channels, strong MFA/step-up verification, audited break-glass access, notifications, and revocation of active administrator sessions after recovery. A normal participant account must never be promoted into an administrator account through password recovery.

## Security and operational gates

- Use role-based access control on every backend endpoint; hiding frontend links is not access control.
- Enforce MFA for administrators before exposing cross-participant data or financial approval actions.
- Require step-up authentication for high-impact actions and short-lived administrator sessions.
- Separate read-only analytics permissions from participant-support permissions and financial-approval permissions.
- Apply least-privilege database access, audit-log integrity controls, retention rules, and privacy safeguards.
- Aggregate reporting must disclose its scope, currency, time window, and whether values are paper-only or settled real-money figures.
- No single administrator dashboard total may blend ZAR, USD, EUR, GBP, or KWD without a clearly disclosed conversion methodology and timestamp. Prefer separate currency totals.
- The administrator interface must label paper balances as virtual and non-withdrawable.
- AI/market intelligence must not approve withdrawals, change user roles, access credentials, or bypass the Risk/Execution boundary.

## Delivery sequence

1. Provisioned administrator role and separate secure login, with MFA and audit trail.
2. Participant directory, Standard/Premium entitlement visibility, and safe read-only dashboard inspection.
3. Platform-wide operational and paper-trading analytics with explicit currency/ledger boundaries.
4. Account recovery flow with token hashing, expiry, one-time consumption, session revocation, notifications, and tests.
5. Withdrawal request and first-withdrawal approval workflow only after legal, compliance, provider, custody, security, and reconciliation launch gates pass.

This document records requirements and sequencing. It does not claim these capabilities or real-money transfers are already implemented.
