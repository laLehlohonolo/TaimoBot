# Financial Segment — Product Requirements and Safety Boundary

**Status:** Product requirement / design only. This document does not authorise real-money operation.

## 1. Product decision

TaimoBot must keep each participant's real-money wallet completely separate from their paper/demo trading account.

- The real-money wallet represents actual funds received and payable through an approved payment arrangement.
- The demo account uses virtual ZAR-denominated credit only.
- Demo orders, losses, profits, resets, bonuses and performance statistics must never debit, credit, reserve, transfer or otherwise alter real-money balances.
- Real-money deposits and withdrawals must never be affected by demo trading, a demo reset, market-data outages, trading suspension or paper-account losses.
- UI labels must make the distinction unmistakable: **REAL MONEY** versus **DEMO — VIRTUAL FUNDS**.

## 2. Proposed demo allowance

Initial proposed demo credit: **R100,000 virtual ZAR per participant**.

This is a simulation allowance, not a deposit, loan, investment, entitlement to cash, or promise of returns.

- Show the demo balance and demo P&L only in the demo area.
- Demo losses may reduce the virtual balance to zero; they must not create debt or a negative real-money balance.
- A demo reset/top-up must be an explicit demo-only action and must be audited.
- No demo balance or profit may be withdrawn, transferred, converted to cash, or used to satisfy a real-money payment.
- Keep demo funding configurable so the allowance can be changed without touching wallet records.

## 3. Currency choice and multi-currency balances

Participants must be able to choose a preferred balance display currency from:
- South African Rand — `ZAR`
- US Dollar — `USD`
- Euro — `EUR`
- Pound Sterling — `GBP`
- Kuwaiti Dinar — `KWD`

The product must support two distinct capabilities in stages:

**Stage A — one-time display-currency selection**
- Each participant chooses exactly one preferred display currency from ZAR, USD, EUR, GBP or KWD during the designated first-time setup.
- The choice is one-time and permanent: after the participant confirms and saves it, they cannot change it again through profile settings, wallet settings, support requests or a later self-service flow.
- Show a clear warning before confirmation: "You can choose this currency only once. You will not be able to change it later." Require an explicit final confirmation.
- Persist the selection as an immutable participant preference with the selection timestamp and confirmation/audit record. Enforce one selection per participant in the backend/database; do not rely only on a disabled frontend selector.
- If no choice has been made, prompt the participant to choose before enabling the balance display. Do not silently choose a default on their behalf.
- The displayed original wallet balance and its actual currency remain clearly visible.
- If a balance is shown in another currency, label it as an indicative converted value and show the exchange-rate source and timestamp.
- A display preference must never create a new currency balance, execute an FX conversion, change a transaction's original currency, or move/deduct funds.
- The one-time display-currency choice remains separate from any later multi-currency wallet balances; it does not prevent the participant from viewing individually held balances in their actual currencies.
- The one-time display-currency choice remains separate from any later multi-currency wallet balances; it does not prevent the participant from viewing individually held balances in their actual currencies.
- If a reliable rate is unavailable or stale, show the original balance and explain that conversion is temporarily unavailable; do not invent or silently reuse a rate.

**Stage B — separate multi-currency wallets**
- The long-term wallet model should allow a participant to hold separate balances in each supported currency, subject to provider, legal and operational approval for that currency.
- Each currency balance is its own ledger account and is never silently netted against another currency.
- Deposits and withdrawals must preserve the actual transaction currency and provider settlement details.
- Moving value between currencies is a separate, explicit FX transaction with a disclosed quote, rate, fees/spread, expiry and participant confirmation. Post both sides atomically with auditable linked journal entries; never implement FX as a display-only recalculation.
- Availability of KWD (or any other currency) for actual deposits, custody, conversion or payouts must be confirmed with the chosen provider and reviewed before being offered as a funded wallet.

Currency precision must follow the currency's minor-unit exponent. ZAR, USD, EUR and GBP use two decimal places; KWD uses three. Store monetary values as integer minor units for the transaction currency, never binary floating-point. Do not assume every currency has two decimal places.

## 4. Real-money wallet experience

When the financial segment is authorised and launched, participants should be able to view:

- Actual settled wallet balance.
- Available balance.
- Pending deposits and withdrawals.
- Funds reserved for a pending transaction, if applicable.
- Complete, downloadable transaction history with references, timestamps, fees and statuses.

Never display simulated/demo amounts as real funds. Do not describe a pending deposit as settled cash.

## 5. Deposits must remain independent

Deposits must use a supported payment-provider integration and an auditable transaction lifecycle.

- A demo trade, loss, reset or system maintenance event must not cancel, reverse, delay or modify a real deposit.
- Use provider references and idempotency keys to prevent duplicate credits.
- Credit only after independently verified provider confirmation; never trust a browser success screen.
- Record pending, confirmed, failed, reversed and reconciled states.
- Reconcile provider settlements against the internal ledger.
- A failed or delayed payment must remain visible with a clear status and support reference; do not silently discard it.

## 6. Withdrawals to banks

The product goal is broad South African bank coverage, not an unverified promise that every bank is supported from day one.

- Integrate a licensed/approved payment provider whose payout coverage is verified for each supported bank and account type.
- Verify participant identity and bank-account ownership before payout.
- Show the withdrawal amount, any fee, destination bank/account (masked), status, reference and expected processing window.
- Use idempotency and transaction-state controls to prevent duplicate payouts.
- Support pending, approved, processing, paid, failed, returned and cancelled states as appropriate to the provider.
- Never mark a withdrawal paid until the provider confirms settlement.
- Do not let demo trading or demo losses reduce the withdrawable wallet balance.

## 7. Ledger and system architecture requirements

Before implementation, design a dedicated real-money double-entry ledger with immutable journal entries. Wallet balances must be derived/reconciled from ledger entries, not edited as arbitrary numbers.

Minimum controls:
- Separate database tables/models, service boundaries and permissions for real-money ledger and demo ledger.
- Atomic posting and reversal entries; never delete or overwrite financial history.
- Unique provider transaction IDs and idempotency keys.
- Explicit transaction state machine and concurrency protection.
- Daily provider/bank reconciliation, exception queue and auditable operator actions.
- Role-based access, strong authentication, encryption, secrets management, monitoring and tamper-evident audit logs.
- No admin action may silently manufacture, erase or transfer customer funds.
- No trading engine, strategy, AI or paper-execution path may call wallet-credit/debit operations.

## 8. Launch gates — no real-money activation before all are met

1. South African legal and regulatory review confirms the exact business model, licences/authorisations and permitted activities.
2. Payment-provider and banking partners approve the use case, settlement flow, supported banks, fees, limits, refunds and chargeback handling.
3. Required customer identity, AML/FICA, sanctions, fraud, privacy/POPIA and record-retention controls are reviewed and implemented where applicable.
4. Customer-funds safeguarding, custody/segregation, insolvency treatment and reconciliation arrangements are independently confirmed.
5. Security review, penetration testing, operational runbooks, incident response and customer-support procedures are complete.
6. End-to-end sandbox tests prove deposit, duplicate webhook, delayed settlement, reversal, withdrawal, payout failure, reconciliation and account closure scenarios.
7. Explicit product-owner approval is recorded before any production real-money credentials or payment routes are enabled.

Until these gates are met, real-money deposit/withdrawal functionality remains **NOT IMPLEMENTED / DISABLED**. The current TaimoBot remains paper-trading only. This document is a product/engineering checklist, not legal advice or a determination of regulatory status.
