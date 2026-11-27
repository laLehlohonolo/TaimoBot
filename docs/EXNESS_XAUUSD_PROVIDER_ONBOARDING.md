# Exness XAUUSD Candidate: Provider Onboarding and Verification Gate

## Decision

Exness is the selected first-choice broker candidate for evaluating TaimoBot's XAUUSD market-data integration. IC Markets remains the fallback candidate.

This is a candidate selection only. It does not register a provider, establish a live connection, verify an account, or authorize trading.

## Public information checked (2026-11-27)

The following facts are published by Exness and the FSCA:

- Exness South Africa identifies Exness ZA (Pty) Ltd as an FSCA-authorised financial services provider, FSP 51024.
- Exness South Africa's legal disclosures state that Exness ZA acts as intermediary and that accounts opened through that site are registered with Exness (SC) Ltd, the product supplier.
- Exness identifies Exness (SC) Ltd as authorised by the Seychelles FSA and as an OTC Derivatives Provider authorised by the FSCA in South Africa.
- The FSCA's public OTC Derivative Providers search lists EXNESS SC (LTD) with status Approved.
- Exness publishes XAUUSD availability on MT5 and lists a 100-troy-ounce contract size and 0.01-lot minimum volume for the described XAUUSD instrument. Account types, trading commissions, swap schedules, stop levels, margin and execution conditions vary and must be verified against the actual demo account and its server-side symbol specification.
- Exness documents MT5 demo accounts with virtual equity. Demo conditions are described as similar to real-account conditions, but this is not proof that every execution or cost characteristic is identical.

These public disclosures are not legal advice and do not establish which entity, terms, or protections apply to a particular user's account. Record the legal contracting entity from the account's own agreement and Personal Area before any later regulated or real-money assessment.

## Evidence sources

- Exness regulation: https://www.exness.com/regulation/
- Exness South Africa platform and entity disclosures: https://www.exness.co.za/trading-platforms/
- Exness South Africa legal documents: https://www.exness.co.za/legal-documents/
- Exness XAUUSD instrument information: https://www.exness.co.za/commodities/xauusd/
- Exness commodities specifications: https://get.exness.help/hc/en-us/articles/17854173039388-Commodities
- Exness demo and real accounts: https://get.exness.help/hc/en-us/articles/360013191919-Real-and-demo-accounts
- FSCA OTC Derivative Providers search: https://prod-entitysearchwebapplication.azurewebsites.net/otc-derivative-providers

## Required onboarding evidence

Do not set any Market trust flag to true merely because Exness was selected. Each gate needs independently reviewed evidence:

1. **Provider identity and legal entity**
   - Capture the exact MT5 server name and account type from the demo account.
   - Capture the legal contracting entity and agreement applicable to that account.
   - Reconcile the account entity with the current regulator disclosures.
2. **Source and connectivity**
   - Establish the actual quote source and permitted data-use terms.
   - Demonstrate a stable, authenticated, server-to-server read-only feed.
   - Record provider version, server time, receive time, heartbeat and stable evidence IDs.
3. **Symbol mapping**
   - Read the exact symbol from MT5 Market Watch / symbol metadata.
   - Do not assume the symbol is exactly XAUUSD; suffixes or account-specific variants must be explicitly mapped and tested.
4. **Broker/instrument specifications**
   - Capture contract size, tick size, tick value, digits, volume minimum/maximum/step, stop/freeze levels, margin rules, trading sessions and order calculation mode from the actual demo server.
   - Capture spread, commission, swap/financing and applicable holiday/session changes.
   - Reconcile the server metadata with official instrument documentation; record discrepancies rather than silently choosing one.
5. **Market data quality**
   - Test timestamp timezone, bid/ask validity, freshness, cadence, duplicates, gaps, outliers, reconnect behavior and historical coverage.
   - Keep provider time and service receive time separately.
   - A chart screenshot or one successful quote is not sufficient evidence of feed health.
6. **Security and operations**
   - Use a dedicated least-privilege demo account and a read-only adapter. Never place trading credentials in source code, GitHub, logs, frontend storage, or chat.
   - The adapter must expose quote/symbol/heartbeat reads only. It must not expose order, modify, close, deposit, withdrawal or account-balance mutation methods.
   - Protect the service-to-backend evidence channel with authenticated transport, request freshness, replay protection and audit logging.

## TaimoBot implementation boundary

- The current environment-backed provider registry is configuration scaffolding only. It does not connect to MT5 or ingest evidence.
- Do not set `TAIMOBOT_MARKET_PROVIDER_ID` or any `TAIMOBOT_MARKET_*_VERIFIED` trust flags in a deployment until the corresponding evidence and adapter have been reviewed.
- Do not infer or hardcode account-specific contract values as universal constants. Persist a versioned specification snapshot with its source and capture time.
- The first adapter milestone is read-only quote and symbol-metadata collection from a dedicated Exness MT5 demo environment, plus a signed/authenticated evidence handoff to the backend.
- Keep the current Deriv synthetic-index collector separate. It must never be used as evidence for XAUUSD.
- MARKET remains NOT_IMPLEMENTED/BLOCKED until authenticated evidence is connected and passes the existing fail-closed health evaluator.
- A healthy Market data status does not approve a strategy or authorize an order.
- Paper trading remains the only trading mode. No live broker order path, real-money wallet operation, or broker execution permission is introduced.
- The per-trade 1% maximum and account-wide combined open-risk ceiling of 1% remain hard backend controls.

## Acceptance gate before calling the Exness adapter complete

- Correct demo server and legal entity are recorded.
- Actual XAUUSD symbol mapping is independently verified.
- Broker specifications are captured from the demo server and validated.
- Quote and heartbeat evidence is authenticated, timestamped and auditable.
- Tests cover empty, stale, future-dated, malformed, duplicated, gapped and disconnected feed states.
- Reconnects cannot reset freshness or erase gaps.
- Provider payloads cannot self-attest trust flags.
- No signal, paper order, live order, balance mutation or risk-setting mutation can be triggered by the adapter.
- Full backend and frontend CI pass.

## Current status

**Selected candidate: Exness.** **Fallback: IC Markets.** No Exness demo account has been connected or inspected by this implementation. No XAUUSD feed, symbol mapping, broker specification, legal account entity or adapter is yet verified. No Market health promotion or execution permission is granted.


## Read-only MT5 normalization contract (2026-11-27)

Added `backend/app/exness_market_contract.py` with a provider-specific normalization boundary for data collected by a separately controlled MT5 bridge:

- Symbol metadata is accepted only when the server symbol exactly matches an explicitly configured expected symbol; suffixes are never guessed.
- Contract size, point, tick size/value, volume min/max/step, stop/freeze levels, calculation mode and digits are validated before normalization.
- Quotes require finite positive bid/ask values, ask not below bid, a positive integer millisecond provider timestamp and a timezone-aware service receive time.
- Provider time and service receive time are retained separately; evidence IDs are deterministic for a symbol/tick timestamp.
- Every normalized record remains `UNVERIFIED_PENDING_REVIEW` and `execution_eligible=false`.

Focused tests are in `backend/tests/test_exness_market_contract.py`. This is a pure validation/normalization module, not an MT5 connection, authenticated evidence adapter, durable observation store or broker verification. It has no order methods and does not ingest credentials. The next gate is to run CI, then implement and security-test an authenticated read-only evidence handoff only after the actual demo server/account environment is available.
