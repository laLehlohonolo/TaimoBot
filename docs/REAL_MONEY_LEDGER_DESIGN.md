# Real-Money Ledger — Technical Design (Not Activated)

**Status:** Architecture specification only. No wallet endpoints, payment provider, bank integration, real-money credentials or production ledger are implemented by this document.

## 1. Non-negotiable boundary

The existing TaimoBot paper-trading subsystem remains unchanged and remains the only active financial/trading mode.

The real-money subsystem, when separately authorised, must be isolated from:
- `paper_accounts`, `trades`, position groups and copy-trading records;
- strategy, AI, signal, execution and market-data services;
- demo-credit issuance, resets, demo P&L and demo performance rankings.

No code in the paper execution path may import or invoke a real-money wallet service. Demo funds and demo profits have no cash value and are never withdrawable.

## 2. Proposed data model

Use integer minor units (South African cents) for all ZAR amounts. Do not use binary floating-point for money.

### `money_accounts`
- `account_id` (opaque primary key)
- `owner_user_id` (foreign key to participant)
- `currency` (ISO currency code; initially ZAR only)
- `account_type` (participant liability, provider clearing, fee revenue, suspense, etc.)
- `status` (OPEN, FROZEN, CLOSED)
- `created_at`

A participant may have one ZAR wallet per legal product/account policy. Enforce uniqueness at the database level. Provider clearing, fee and suspense accounts are system-controlled, not participant wallets.

### `money_journals`
- `journal_id` (primary key)
- `transaction_id` (foreign key)
- `currency`
- `status` (POSTED, REVERSED)
- `created_at`
- `reversal_of_journal_id` (nullable; unique when present)

A posted journal is immutable. Corrections use a new compensating journal; never edit or delete posted entries.

### `money_journal_lines`
- `line_id` (primary key)
- `journal_id` (foreign key)
- `account_id` (foreign key)
- `side` (DEBIT or CREDIT)
- `amount_minor` (positive integer)
- `created_at`

For every journal and currency, total debits must equal total credits. Enforce the rule in the posting service within one database transaction and add independent reconciliation checks. A journal must have at least two lines.

### `money_transactions`
- `transaction_id` (primary key)
- `owner_user_id`
- `kind` (DEPOSIT, WITHDRAWAL, REFUND, REVERSAL, FEE, ADJUSTMENT)
- `amount_minor` (positive integer)
- `fee_minor` (nonnegative integer)
- `currency` (ZAR initially)
- `status` (state machine below)
- `idempotency_key` (unique per operation scope)
- `provider_id`, `provider_reference` (nullable, unique when supplied)
- `failure_code` (sanitised, nullable)
- `created_at`, `updated_at`, `settled_at`

Never accept client-supplied provider settlement as proof of payment. Only a verified server-to-server provider event or independently authenticated provider query may advance a transaction to a financially settled state.

### `money_holds`
- `hold_id`, `transaction_id`, `account_id`
- `amount_minor` (positive integer)
- `status` (ACTIVE, RELEASED, CAPTURED)
- `created_at`, `released_at`

Withdrawal requests create a hold atomically before payout processing. A hold reduces available funds but does not itself debit the wallet. Failed/cancelled payouts release the hold exactly once; successful payouts capture it through a balanced journal exactly once.

### `money_provider_events`
- `event_id` (primary key)
- `provider_id`, `provider_event_id` (unique composite)
- `provider_reference`
- `event_type`
- `payload_digest` (hash only; minimise stored sensitive payload)
- `verification_status`
- `received_at`, `processed_at`

Store only the minimum provider data needed for audit and reconciliation. Do not store raw card details, banking passwords, PINs or authentication secrets.

### `money_audit_events`
Append-only operator and system audit events with actor, action, target, reason, timestamp and correlation ID. Do not put passwords, access tokens or full bank-account details in audit text.

## 3. Balance calculation

Do not maintain a mutable wallet balance as the source of truth.

- Ledger balance is derived from posted journal lines for the participant liability account using the defined debit/credit convention.
- Available balance = settled ledger balance minus active withdrawal holds and other legally permitted reserves.
- Pending deposits are displayed separately and are not spendable until settled and posted.
- Every account is single-currency. Never net balances across currencies.
- Reconciliation compares ledger totals, transaction state, provider settlement reports and bank settlement records.

Use integer cents, bounded amounts, explicit currency and decimal-safe API serialization. Reject negative, zero, fractional-cent, NaN, infinity and out-of-range amounts.

## 4. Transaction state machines

### Deposit
`CREATED -> PENDING_PROVIDER -> CONFIRMED -> POSTED`

Alternative terminal or exception states: `FAILED`, `EXPIRED`, `REVERSED`, `RECONCILIATION_REQUIRED`.

- Create a pending transaction with a unique idempotency key.
- Verify provider event signature/authentication, event identity, amount, currency, merchant/account reference and expected transaction.
- Post the balanced journal only once after authoritative settlement confirmation.
- A duplicate webhook returns the already-recorded result and never credits twice.
- A reversal is a new compensating journal linked to the original; never erase the original credit.
- Out-of-order, mismatched, unsigned or ambiguous events go to a review queue and do not move funds.

### Withdrawal
`REQUESTED -> IDENTITY_CHECK_PENDING -> APPROVED -> PAYOUT_PENDING -> PAID`

Alternative states: `REJECTED`, `CANCELLED`, `PAYOUT_FAILED`, `RETURNED`, `RECONCILIATION_REQUIRED`.

- Verify identity, account ownership, account status, limits, sanctions/fraud controls and available funds.
- Atomically create a hold and move the request to approved/payout pending.
- Submit a payout using a stable provider idempotency key.
- Mark paid only after authoritative provider settlement confirmation; capture the hold with a balanced journal.
- On verified failure/return, release or reverse the hold according to provider settlement facts.
- Repeated requests, webhooks or retries must not create a second payout or release/capture a hold twice.

Every state transition must be validated server-side and recorded with actor/source, timestamp, reason and correlation ID. Invalid transitions fail closed.

## 5. Strict separation from demo funds

The R100,000 demo allowance remains a proposed configurable virtual-credit policy, not a real-money ledger opening balance.

- Demo credits live only in paper/demo account records and are labelled virtual.
- Demo resets and top-ups do not create `money_transactions` or `money_journals`.
- Real-money deposits do not top up demo credit automatically.
- Demo losses, gains, margin, risk, fees and withdrawals never read or write real-money ledger tables.
- Real-money withdrawal eligibility never includes demo equity, floating P&L or virtual credits.
- A demo account may be reset to its configured virtual allowance without a real-money transaction.

## 6. Security, privacy and operations

Before any real-money build is enabled:
- Complete South African legal/regulatory assessment for the exact service and funds flow.
- Obtain written approval from the chosen payment/banking provider for collections and payouts, and verify bank/account coverage.
- Confirm customer-funds safeguarding, custody, settlement, insolvency treatment and reconciliation responsibilities.
- Complete applicable identity verification, FICA/AML, sanctions, fraud, POPIA, retention and consumer-disclosure requirements.
- Apply least-privilege roles, MFA for sensitive operator actions, encryption in transit/at rest, managed secrets, key rotation and security monitoring.
- Separate production payment credentials and deployment permissions from paper trading.
- Daily three-way reconciliation: internal ledger, provider settlement report and bank settlement.
- Exception queues, maker-checker approvals for manual corrections, dispute handling, incident response and customer support.
- Never allow silent balance edits, direct SQL balance changes, or operator-created customer credits outside a controlled, approved journal workflow.

## 7. Required pre-production tests

Use a provider sandbox only after legal/provider approval. Tests must include:
1. Balanced journal invariant and integer-cent arithmetic.
2. Concurrent duplicate deposit webhooks credit exactly once.
3. Invalid signature, wrong amount, wrong currency and unknown reference never credit.
4. Delayed, duplicated, out-of-order and missing provider events.
5. Deposit reversal creates a compensating journal without deleting history.
6. Withdrawal holds prevent overspending under concurrent requests.
7. Duplicate payout submission uses the same provider idempotency key.
8. Payout success captures one hold and posts one journal.
9. Payout failure/return releases or reverses exactly once.
10. Reconciliation identifies missing, duplicate and mismatched settlement records.
11. Participant A cannot read or operate participant B's wallet.
12. Demo reset, demo profit/loss and paper trades leave all real-money tables byte-for-byte unchanged.
13. Real-money ledger code cannot be called by paper execution, copy trading, strategies or AI.
14. Account freeze, closure, refund, dispute and audit export scenarios.

## 8. Activation gate

This design is not permission to accept deposits or pay withdrawals. Implementation remains disabled until the product owner has approved a compliance-reviewed design, provider contract and sandbox plan. Production activation requires a separate explicit approval after independent security, financial reconciliation and regulatory sign-off.
