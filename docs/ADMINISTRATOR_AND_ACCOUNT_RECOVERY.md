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

## Participant registration and verification

Participant registration must collect the following before an account is considered registration-complete:

1. Legal first and last names, matching the participant's identity document.
2. South African identity number for South African applicants. If TaimoBot later accepts non-South-African participants, use a country-specific identity-document flow rather than assuming every person has a South African ID number.
3. Residential address, including the fields needed to identify the physical address. Do not treat a typed address alone as independently verified proof of residence.
4. Email address, with a verification link sent to that exact registered address.

Email verification flow:

- Generate a cryptographically random, single-use email-verification token and send a HTTPS link to the registered email address.
- Store only a secure hash of the token, its purpose, account reference, expiry and consumed timestamp.
- Set a reasonable short expiry (target: 24 hours); allow a rate-limited resend that invalidates prior unused links.
- Mark email as verified only after the link is opened and the token is atomically validated and consumed.
- Keep the account in an unverified/pending state until email verification is complete. Use neutral responses and rate limits to reduce account enumeration and abuse.
- Never send passwords, ID numbers or full identity documents in the verification email or URL.

Identity and address checks:

- Validate the ID number's format and checksum where applicable, but do not treat format validation as proof that the identity is genuine or belongs to the applicant.
- Where identity or address verification is legally required for a feature, use an appropriately authorised identity-verification/KYC provider and/or documented manual review. Record the provider, check type, result, timestamp and reference, not unnecessary copies of sensitive source data.
- Define acceptable proof-of-address documents, freshness rules and exception handling with the compliance provider/legal review before enabling the relevant financial features. A submitted residential address is a registration field; it is not automatically verified merely because it was entered.
- Keep separate status fields for email verification, identity verification and address verification. Do not collapse them into one generic "verified" flag.
- Until checks are complete, restrict access to the features that require verification. Do not enable real-money deposits or withdrawals based only on email verification.
- Encrypt sensitive identity data at rest and in transit, mask it in ordinary screens and logs, restrict access by role, audit every authorised view, and apply a documented retention/deletion schedule under applicable privacy law. Never expose full ID numbers in administrator analytics.
- Obtain and record appropriate privacy notices and lawful processing grounds before collecting identity data. Do not collect more data than the specific product and legal purpose requires.

Registration status should therefore distinguish at least: EMAIL_PENDING, EMAIL_VERIFIED, IDENTITY_PENDING, IDENTITY_VERIFIED, ADDRESS_PENDING, ADDRESS_VERIFIED, REVIEW_REQUIRED, and REJECTED. Status transitions must be auditable and must not be self-assigned by the participant or frontend.

## Participant reference numbers and product-key activation

Every participant must receive a unique, permanent TaimoBot participant reference number when their account is created. It is generated by the backend, is non-guessable where it is used in public-facing workflows, cannot be selected or changed by the participant, and is never reused after account closure. It is an account identifier, not a password, secret, proof of identity, payment receipt, or trading credential.

Demo registration and paper trading:
- A participant may register a Standard or Premium demo account without entering or purchasing a product key.
- After completing the applicable account-registration and email-verification steps, the participant can access the demo experience without a product key.
- Demo balances, gains and losses remain virtual. A participant reference number or product key never converts demo funds or demo profits into withdrawable money.

Real-money feature activation:
- A separate, unique product/activation key is required before a participant can request activation of any future real-money account or money-handling features.
- The activation key must be issued through an authorised TaimoBot process, securely generated, bound to the intended participant or account, and validated by the backend. It must not be a shared generic code.
- Keys must have explicit status and lifecycle (for example: ISSUED, CLAIMED, EXPIRED, REVOKED), purpose, issue time, expiry where applicable, and an auditable issuer/claim record. Claiming must be atomic so the same key cannot activate multiple accounts. Store only a secure hash of the secret key; never expose it in logs or administrator analytics.
- A key claim must not silently change a participant's Standard/Premium tier, wallet currency, risk limit, or identity-verification status. Those are separate controlled decisions.
- A valid key is only an eligibility/activation prerequisite. It is not proof of identity, email, residential address, KYC/AML/FICA compliance, beneficiary ownership, regulatory permission, or available funds. It does not guarantee earnings, returns, trades, withdrawals, or account approval.
- Before real-money features can be enabled, the participant must independently pass all applicable email, identity, address, compliance and eligibility checks, and TaimoBot must satisfy the legal, regulatory, payment-provider, safeguarding, privacy, security, reconciliation and operational launch gates. Until then, real-money functionality remains disabled regardless of key status.
- Provide safe recovery/support handling for lost, compromised, mistakenly issued, expired or revoked keys. Administrators may revoke or reissue only through a permission-checked, reason-recorded, audited workflow. Never reveal an existing secret key; issue a replacement under controlled policy.
- Do not collect payment for a product key or promise a financial return unless a separately approved commercial, legal and regulatory model explicitly permits it. No key-sale, real-money activation or cash-transfer capability is implemented by this requirement.

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
