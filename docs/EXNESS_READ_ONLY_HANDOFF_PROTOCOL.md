# Exness Read-Only Evidence Handoff Protocol

## Status

Design contract only. This document does not implement an HTTP endpoint, MT5 bridge, credential store, authenticated provider, or market-observation ingestion. The Exness adapter remains unimplemented and XAUUSD remains unverified. Do not set Market trust flags or promote Market health based on this document.

## Security objective

Define a narrow server-to-server boundary for a separately operated MT5 demo collector to submit read-only market evidence to TaimoBot. The collector must not be able to submit orders, mutate accounts, alter risk settings, approve its own provenance, or change provider trust configuration.

The first deployment target is a dedicated Exness MT5 demo environment. No live-account credentials or trading permissions are required or permitted for this collector.

## Transport and authentication

A future implementation must satisfy all of the following:

1. Accept requests only over HTTPS with certificate validation. Do not expose the handoff endpoint to browser clients or the public participant API.
2. Authenticate a dedicated collector identity using HMAC-SHA256 over the exact raw request body. Do not sign a parsed-and-reserialized JSON object.
3. Require these headers: `X-Taimo-Key-Id`, `X-Taimo-Timestamp`, `X-Taimo-Nonce`, and `X-Taimo-Signature`.
4. Construct the signed message as the UTF-8 bytes of:
   `v1\\n{key_id}\\n{timestamp}\\n{nonce}\\n{sha256_hex(raw_body)}`.
5. Compare signatures with a constant-time comparison. Reject unknown, disabled, expired, or not-yet-valid key IDs.
6. Keep HMAC keys in a managed secret store or deployment secret environment. Never put keys in source control, API responses, logs, frontend storage, or chat. Support planned key rotation with a bounded overlap and explicit revocation.
7. Limit request body size, JSON nesting, field lengths, numeric ranges, and request rate. Reject unknown top-level fields unless a versioned schema explicitly permits them.

The timestamp must be an integer Unix time in seconds and fall within a small configured clock-skew window. The nonce must be cryptographically random, bounded in length, and unique for the authenticated key. Enforce replay prevention with a persistent database uniqueness constraint on `(key_id, nonce)`; an in-memory cache alone is insufficient. Record accepted and rejected request metadata without recording signatures, secrets, or full sensitive payloads.

## Permitted evidence payloads

The initial protocol permits only versioned read-only evidence messages:

- `tick`: exact server symbol, bid, ask, provider timestamp in milliseconds, collector receive timestamp, and stable source/evidence identifier.
- `symbol_specification`: exact server symbol, contract size, point, tick size/value, digits, volume minimum/maximum/step, stop/freeze levels, calculation mode, and capture timestamp.
- `heartbeat`: provider identity, collector version, MT5 server identifier, connectivity state, check timestamp, and latest observation timestamp.

Every message must include a schema version, provider ID, instrument identifier, exact server symbol, and stable evidence ID. The backend must bind the provider ID to server-owned configuration; the payload cannot register a provider or assert that any trust gate is verified.

The endpoint must reject account passwords, investor passwords, access tokens, account balance/equity, personal identity data, deposit/withdrawal instructions, order requests, position modifications, and arbitrary command fields. Do not accept an instruction to run code or call another URL.

## Validation and persistence

After authentication, the future handler must:

1. Validate the envelope and schema before any write.
2. Require provider ID and canonical instrument to match server-owned allowlists.
3. Require exact symbol mapping already configured by an operator; never infer suffixes or aliases.
4. Normalize tick and specification fields through `backend/app/exness_market_contract.py`.
5. Preserve provider time and service receive time separately.
6. Persist evidence idempotently using stable evidence identity, with uniqueness constraints that prevent duplicate observations from inflating coverage.
7. Persist a monotonic accepted sequence or equivalent cursor per provider/symbol so delayed or out-of-order messages cannot silently roll health backwards.
8. Keep raw payload retention minimal and bounded. Store a payload hash and normalized evidence where possible.
9. Audit key ID, provider, message type, evidence ID, receive time, validation outcome, and rejection reason. Do not log secrets or full signed bodies.

A valid HMAC proves possession of the configured collector key; it does not prove that the quote source, legal entity, broker specification, symbol mapping, or data-use rights are correct. Those remain separate operator-reviewed trust gates.

## Failure behavior

- Missing or invalid authentication: reject without storing market evidence.
- Expired timestamp or replayed nonce: reject and audit the reason.
- Invalid JSON, schema, signature, symbol, timestamp, quote, or specification: reject without partial writes.
- Duplicate evidence ID: return an idempotent duplicate result; do not increase coverage.
- Out-of-order or stale evidence: preserve diagnostics but never replace a newer latest-observation pointer.
- Unknown provider or instrument: reject; do not auto-register.
- Database or audit persistence failure: fail the request; do not report acceptance.
- Collector disconnect or heartbeat expiry: Market health must degrade/offline according to the existing evaluator; no stale HEALTHY status may persist.

## Strict capability boundary

The handoff service is read-only. It must have no route or code path for MT5 login actions, order submission, order modification, position closing, deposits, withdrawals, account mutation, signal approval, strategy deployment, risk-limit changes, or emergency-stop deactivation.

All normalized evidence must remain `UNVERIFIED_PENDING_REVIEW` until separate operator review. Even a verified healthy market feed does not authorize a strategy or paper order. Paper trading remains separate, and the hard 1% per-trade and account-wide combined open-risk limits remain unchanged.

## Acceptance tests before implementation can be called complete

- Valid signature and valid schema accepted.
- Tampered body, invalid signature, unknown key, disabled key, and expired key rejected.
- Timestamp outside allowed skew rejected.
- Reused nonce rejected across process restart, proving persistence-backed replay protection.
- Concurrent duplicate nonce submissions permit at most one acceptance.
- Unknown fields and prohibited payload types rejected.
- Wrong provider, instrument, or exact server symbol rejected.
- Malformed, crossed, non-finite, future-dated, and stale quotes rejected or retained only as explicit non-healthy diagnostics.
- Duplicate evidence does not inflate coverage.
- Out-of-order messages cannot roll back latest-observation state.
- Failed database/audit writes do not return accepted status.
- Participant authentication cannot access the handoff or administrator provider internals.
- The adapter cannot invoke trading, account, strategy, or risk mutation paths.
- Key rotation and revocation tests pass.
- Full backend and frontend CI pass.

## Current implementation status

- Exness selection: candidate only.
- MT5 demo server and account: not inspected by this code.
- MT5 collector: not implemented.
- Authenticated handoff endpoint: not implemented.
- Persistent replay protection for this handoff: not implemented.
- XAUUSD feed, source, mapping, legal entity, and broker specifications: unverified.
- Market health promotion and broker execution: disabled.
- Trading mode: paper only.
