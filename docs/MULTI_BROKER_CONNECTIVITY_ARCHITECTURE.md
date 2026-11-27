# TaimoBot Multi-Broker Connectivity Server

## Decision and scope

TaimoBot must not depend on the founder's personal broker account, a single broker, or a single demo account. The platform will operate a central, provider-neutral Broker Connectivity Server. Each participant links their own eligible broker account through an approved connector. Exness is one candidate, not the system's foundation.

This is an architecture and delivery contract for the existing React + FastAPI application. It does not claim that broker connections are already implemented. The current trading mode remains paper-only; live order routing is out of scope and must remain disabled.

## Product objective

- A participant can select a supported broker, connect their own account, and see only their own connection and account evidence.
- Many participants can connect independently and concurrently.
- A participant may have more than one broker connection, subject to explicit product entitlements and broker terms.
- One broker outage or participant disconnection must not affect other participants.
- Adding a broker must mean implementing and validating an adapter, not rewriting the core Market, Strategy, Risk, or Execution layers.
- Broker account credentials must never be requested in chat, placed in source control, exposed to the browser, or written to ordinary application logs.

## Central service architecture

The existing FastAPI backend becomes the control plane for a broker connectivity service with these components:

1. **Broker catalogue** — lists broker brands, legal-entity verification status, supported connection protocols, supported platforms, supported instruments, regions, and capability status. A listing is not an endorsement or proof that a particular participant is eligible.
2. **Participant connection manager** — creates, pauses, reconnects, revokes, and inventories broker connections scoped to the authenticated participant ID.
3. **Connector workers** — run isolated provider-specific connectors. The first shared connector family is MetaTrader 5 (MT5); later adapters can implement official REST, WebSocket, FIX, or other broker-approved APIs.
4. **Credential boundary** — secrets are held only by a dedicated secret manager or participant-controlled terminal/bridge. They are never stored in ordinary participant records or returned by an API.
5. **Evidence gateway** — accepts authenticated, versioned, read-only messages from connector workers; validates provider identity, participant/connection binding, timestamps, nonces, symbol mapping, quotes, and specifications.
6. **Normalization layer** — converts provider-specific messages into canonical TaimoBot market, account, and instrument records while retaining original provider identifiers and provenance.
7. **Observation store and health evaluator** — stores idempotent evidence and evaluates freshness, continuity, gaps, source provenance, and connection health separately for each participant connection and instrument.
8. **Admin operations and audit** — gives authorized administrators fleet-level service health and redacted diagnostics. Administrators must not see participant passwords, tokens, or unnecessary personal/account data.

The central service is multi-tenant. Every connection, observation, account snapshot, credential reference, heartbeat, error, and audit event must be scoped by immutable `participant_user_id` and `connection_id`. Never trust either identifier supplied in an unsigned payload; derive ownership from the authenticated session or authenticated connector identity.

## Connection model: support participants, not one founder account

Preferred first MT5 path:

- A participant selects an MT5-capable broker and creates or uses their own broker demo account.
- The participant runs a signed TaimoBot MT5 bridge beside their own MT5 terminal, or uses a separately provisioned isolated terminal worker where that operating model has been approved.
- The bridge initiates an outbound authenticated connection to TaimoBot. Do not require public inbound ports on the participant's network.
- The MT5 terminal remains the broker session endpoint. The initial bridge exposes read-only market quotes, symbol metadata, heartbeat, and (only if separately approved) read-only account snapshots.
- The bridge cannot place, modify, or close orders. No trade API is exposed in the initial release.
- The central server assigns a unique connection ID and binds the bridge key to one participant and one broker connection. A bridge key cannot be reused across participants.

This model means a participant's own MT5 demo account can test the integration, but no single account is required for server development or for other participants to connect.

For brokers that provide official account-linking APIs, prefer their documented OAuth/authorization-code flow with least-privilege scopes and token revocation. Do not scrape broker websites or automate consumer login pages. Do not assume every broker offers an API suitable for third-party integration.

## Canonical connector contract

Every adapter must implement a provider-neutral interface with explicit capabilities:

- `discover_connection()`
- `health_check()`
- `list_symbols()`
- `get_symbol_specification(symbol)`
- `stream_quotes(symbols)` or `poll_quotes(symbols)`
- `get_historical_market_data(symbol, timeframe, range)` when permitted
- `get_account_snapshot()` only after a separate privacy/security review and explicit participant consent
- `disconnect()`

The first production implementation must not expose order methods. A future order-capable adapter, if ever approved, must be a separate capability package and separately gated service. It must not be enabled by the presence of a market-data connector.

Canonical evidence must preserve:
- broker brand and exact contracting/legal entity where known;
- broker server and exact server symbol;
- canonical instrument and mapping version;
- provider timestamp and TaimoBot receive timestamp;
- bid/ask or OHLCV values and source evidence ID;
- contract size, tick size/value, digits, volume limits/step, stop/freeze levels and calculation mode where available;
- adapter version, schema version, connection ID and health state;
- provenance status and operator review status.

A broker's XAUUSD, GOLD, XAUUSDm, or other symbol must never be inferred from its name. Exact server-symbol mappings are reviewed and versioned per broker server/account environment.

## Multi-tenant security and resilience

- Authenticate participant API requests with the existing session layer and enforce ownership on every read/write.
- Authenticate bridge traffic using per-connection credentials, short timestamp windows, cryptographic nonces, HMAC or mutual TLS, constant-time signature comparison, and persistent replay prevention.
- Use unique database constraints for evidence IDs and replay nonces. An in-memory replay cache is insufficient.
- Store secrets outside the application database in a managed vault; persist only a secret reference and rotation metadata.
- Encrypt sensitive connection metadata at rest; redact account numbers and personal details in logs.
- Apply rate limits, payload-size limits, schema validation, key rotation/revocation, and connection-level kill switches.
- Use per-connection worker isolation, bounded queues, backpressure, timeouts, circuit breakers, and reconnect jitter.
- A failed connector must not block another participant's connection or the paper-trading API.
- Enforce data retention and participant disconnect/deletion workflows.
- Admins can inspect operational health, not retrieve participant broker passwords or tokens.

## Broker options and rollout

The platform should support broker options by capability, not by a hard-coded single-brand dependency.

### Initial candidate set for technical evaluation

| Candidate | Initial connection route | Current classification |
|---|---|---|
| Exness | MT5 bridge; evaluate official API options separately | First candidate, not connected or verified |
| AvaTrade | MT5 bridge; evaluate official API options separately | Candidate; official South Africa platform material lists MT4/MT5 |
| Vantage | MT5 bridge; evaluate official API options separately | Candidate; official material lists MT5 and a South African FSP entity |
| FP Markets | MT5/cTrader route subject to account-region verification | Candidate; verify actual entity, platform and connection terms |
| Tickmill | MT5 bridge subject to account-region verification | Candidate; verify actual entity, platform and connection terms |
| HFM | MT5 bridge subject to account-region verification | Candidate; verify local intermediary/product-supplier structure and account platform |

The table is a technical shortlist, not a regulatory approval, recommendation, guarantee of API access, or claim that all listed services are available to every South African participant. Broker groups may onboard a participant under an offshore entity even where a related South African entity holds an FSCA licence. Before enabling a broker in the participant catalogue, independently verify the exact legal entity, licence/ODP status where applicable, region eligibility, platform, XAUUSD availability, data-use terms, API/bridge permissions, and demo/live account conditions.

IC Markets may remain an additional international technical candidate only after its South African onboarding eligibility, contracting entity, data rights, and integration terms are checked. Do not treat it as a locally regulated default.

## Participant account isolation

A participant's broker connection is not the same thing as a TaimoBot paper account or wallet.

- Broker connections have independent connection IDs and isolated market/account evidence.
- Paper accounts retain their own balances, settlement currency, positions, risk, and history.
- Broker account snapshots, if enabled later, are read-only external evidence and must never overwrite paper balances.
- Real-money wallet/deposit/withdrawal functions remain separate and disabled until all legal, regulatory, safeguarding, KYC/AML/FICA, payment, privacy, security, reconciliation, and operational gates pass.
- Do not silently reuse subscription account limits as broker-connection limits. Define and enforce broker-link entitlements explicitly.
- One participant must never be able to query another participant's connection by guessing a connection ID.

## Market and risk boundaries

- XAUUSD remains the initial target instrument, but each broker/server symbol mapping and contract specification must be independently verified.
- Provider health is evaluated per broker connection and instrument. One healthy participant connection does not prove every participant's connection is healthy.
- Deriv synthetic-index observations remain separate and cannot prove XAUUSD health.
- No connector can generate signals, submit orders, mutate balances, alter risk, or bypass emergency stop.
- Paper trading remains the only trading mode.
- The hard per-trade risk maximum and account-wide combined open-risk ceiling remain 1%; no broker's leverage or margin setting may weaken these limits.
- Broker-side spread, commission, swap, margin, stop/freeze levels and trading sessions must be included in instrument-specific paper simulation only when their provenance is known. Unknown costs must remain visibly unknown, not assumed to be zero.

## Delivery phases and acceptance gates

### Phase 1 — Core server and broker catalogue
- Implement a provider-neutral broker catalogue and connection state model. (Catalogue and initial state model implemented.)
- Add participant-scoped connection lifecycle APIs (create intent, list, pause, resume, revoke) without accepting broker passwords. (Implemented as intents only; no broker session is opened.)
- Add immutable participant/connection ownership and audit records. (Owner-scoped persistence is implemented; a separate append-only audit event stream remains outstanding.)
- Add tests for cross-participant access denial, duplicate connection IDs, revoked keys, and tier entitlement enforcement once policy is confirmed. (Cross-participant ownership denial and lifecycle tests added; key/replay tests await the bridge.)

### Phase 2 — Shared MT5 read-only bridge
- Build one MT5 bridge protocol reusable across compatible brokers.
- Add per-connection authentication, persistent replay protection, quote/symbol/heartbeat ingestion, and isolated workers.
- Validate exact broker server, account type, symbol, and specifications per connection.
- Test multiple participants and multiple brokers concurrently using isolated fixtures; no founder-owned account required.

### Phase 3 — Additional broker adapters
- Add official REST/WebSocket/FIX adapters only where a broker documents and permits them.
- Reuse canonical normalization and health evaluation.
- Maintain broker-specific conformance suites and capability flags.

### Phase 4 — Controlled paper simulation
- Consume verified market observations for paper simulation only.
- Reconcile each participant's simulated fills and costs independently.
- Keep external broker account balances and TaimoBot paper balances strictly separate.

### Future order routing (not authorized)
Any future live order routing requires a separate explicit product decision, legal/regulatory review, broker agreements, security architecture, participant consent, operational controls, and independent risk certification. It is not part of this implementation.

## Acceptance criteria for the multi-broker server

- Two or more participant fixtures can connect to different broker fixtures simultaneously without sharing data or credentials.
- One participant can revoke a connection without affecting another participant.
- Cross-tenant reads, writes, evidence injection, replay, and IDOR attempts are denied.
- Invalid signatures, expired timestamps, replayed nonces, unknown providers, malformed payloads, and wrong server-symbol mappings fail closed.
- Duplicate/out-of-order/stale data cannot inflate coverage or roll health backwards.
- Broker-specific failures are isolated by connection.
- No credential is stored in plaintext or returned in API responses.
- No order, balance mutation, wallet, or risk-setting endpoint is reachable from the connector service.
- Full backend and frontend CI pass.

## Current implementation status

- Existing FastAPI backend: present.
- Existing provider-neutral Market health evaluator and server-owned trust configuration: present.
- Exness-specific MT5 normalization: present, still unverified.
- Multi-broker architecture contract: defined here.
- Participant-scoped broker connection-intent lifecycle: implemented (create, list, pause, resume, revoke), with owner-scoped SQLite records and cross-participant denial tests. This does not connect to a broker terminal or feed.
- Broker catalogue module and authenticated read-only `GET /brokers` API: implemented; it lists six candidates only, without claiming any broker is connected or execution-enabled.
- Broker catalogue UI: not implemented.
- MT5 bridge: not implemented.
- Authenticated multi-tenant evidence gateway: not implemented.
- Broker catalogue route authentication and candidate-only response tests: implemented.
- Participant connection-intent isolation and lifecycle API tests: implemented for two participant fixtures. Concurrent live bridge/evidence isolation tests: not implemented.
- Live broker order routing: disabled and out of scope.


## Participant broker connection-intent API (2026-11-27)

Added `backend/app/broker_connections.py` and initialized its owner-scoped SQLite table through the normal database initialization path. Authenticated participants can create a connection intent, list only their own intents, pause, resume, or revoke an intent through the `/broker-connections` routes.

Security and capability boundaries:
- Participant identity is derived from the authenticated session; request bodies cannot nominate an owner or connection owner.
- Connection IDs are generated server-side and every read/update is constrained by both connection ID and authenticated participant ID.
- The create request rejects extra fields, including broker passwords/tokens; no broker credential or secret reference is accepted or stored.
- Broker and platform must match the candidate catalogue.
- The API reports `REQUESTED` / `PAUSED` / `REVOKED` intent state and explicitly reports `NOT_CONNECTED`; it does not establish a broker session or claim an integration exists.
- Live order routing remains false.

Added tests for authentication, two-participant isolation, cross-owner IDOR denial, pause/resume/revoke transitions, revoked-state rejection, unsupported broker/platform rejection, and credential-field rejection.

The participant connection-intent lifecycle is an initial Phase 1 slice, not the complete connection manager/connector. A shared MT5 bridge, outbound participant-side worker, authenticated evidence gateway, persistent nonce replay protection, append-only audit stream, per-connection health and concurrent feed-isolation tests remain outstanding. CI for commit `a6981a263d71683a564b844f71e2cf86a5f63a48` is still running; no pass is claimed yet.
