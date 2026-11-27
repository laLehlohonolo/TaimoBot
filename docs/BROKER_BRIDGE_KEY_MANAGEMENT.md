# Broker Bridge Key Management Contract

Status: design contract only. No vault adapter, key-provisioning API, key rotation service, or bridge endpoint is implemented by this document.

## Security objective

Issue a distinct authentication key to each participant broker connection. A key must authenticate only its own immutable connection. Exchanging or revoking one key must not affect another participant or connection.

The bridge remains read-only. Key possession grants permission to submit only the explicitly supported heartbeat and quote evidence shapes; it never grants account access, trading, balance mutation, or administrative privileges.

## Trust boundaries

1. The authenticated participant requests a broker connection intent.
2. A privileged server-side provisioning workflow verifies that the connection is eligible for bridge onboarding.
3. The server creates a random, opaque key identifier and asks the approved secret manager to generate and store at least 256 bits of cryptographically random key material.
4. The application database stores only non-secret metadata and an opaque vault reference. It never stores key bytes, passwords, investor passwords, access tokens, or recoverable credential material.
5. At request time, the gateway resolves the key identifier to one active connection and retrieves the secret using the service's least-privilege vault identity.
6. The gateway derives participant ownership from the database connection row. It must never accept participant_user_id, connection ownership, or vault references from bridge payloads.

A key identifier is not a secret. A vault reference is not authorization: the server must verify its connection binding and key state before retrieval.

## Key metadata model

Persist only:
- key_id (opaque, unique identifier)
- connection_id (foreign key to one participant connection)
- secret_ref (opaque secret-manager reference)
- status (ACTIVE or REVOKED)
- created_at, activated_at, revoked_at
- created_by / rotation audit actor identifier where available
- key version / rotation sequence

Enforce at most one ACTIVE key per connection with a database uniqueness constraint or an equivalent atomic transaction. A revoked key cannot be reactivated. A key ID cannot be rebound to another connection. A connection in PAUSED or REVOKED state cannot authenticate even if its key metadata remains ACTIVE.

## Provisioning and rotation

- Generate key material inside the secret manager or with a cryptographically secure server-side generator; never accept a participant-supplied key.
- Do not place key bytes in API responses, logs, analytics, traces, exception messages, Git, ordinary database rows, or support tickets.
- Deliver a new key to the participant bridge through a separate authenticated one-time provisioning channel. Do not deliver secrets in ChatGPT messages.
- Rotation creates a new key ID and secret reference, activates the new key atomically, and revokes the prior key. The default policy is no overlap; any temporary overlap requires a separately reviewed, time-bounded migration design.
- Revocation must take effect immediately at the gateway, invalidate cached secret material, and be auditable.
- Revoke keys when a connection is revoked, participant access is removed, compromise is suspected, or key custody cannot be verified.
- Secret-manager outages fail closed. Do not fall back to plaintext database storage or cached keys beyond an explicitly reviewed, short bounded cache policy.

## Gateway authentication requirements

- Resolve key ID from a dedicated authentication header to server-owned key metadata.
- Verify key status, connection state, key-to-connection binding, and participant ownership before accepting evidence.
- Retrieve the secret only after metadata checks.
- Verify HMAC over the canonical connection-bound request, timestamp, nonce, and exact raw-body digest using constant-time comparison.
- Consume the nonce persistently and atomically before evidence persistence.
- Enforce request body size, content type, schema, per-key rate limits, and connection-level circuit breakers.
- Derive connection_id from the key record, not from an untrusted body field. If a connection ID is also supplied in a header for signature construction, it must exactly match the server-resolved connection ID.
- Persist evidence with ownership derived from the database and emit a redacted append-only security audit event.
- Return generic authentication errors that do not disclose whether a key ID, connection, participant, or vault reference exists.

## Operational controls

- Separate provisioning, rotation, revocation, and runtime-authentication permissions.
- Require verified administrator authorization and an auditable reason for operator-initiated provisioning or revocation.
- Do not allow the bridge, participant, or Taimo Brain to provision its own privileged keys or approve its own trust status.
- Monitor failed signatures, replay attempts, clock skew, key lookup failures, vault latency, unusual request rates, and repeated validation errors per connection.
- Apply retention limits to nonce and audit records without weakening replay protection.
- Test concurrent rotations, concurrent nonce submissions, revocation races, cross-tenant key-ID guessing, stale cached secrets, vault outage, and connection pause/revoke behavior.

## Required acceptance tests before an endpoint is enabled

1. Two participants with separate connections and keys cannot cross-authenticate or read/write each other's evidence.
2. A key ID cannot be rebound to another connection.
3. Revoked, unknown, paused, and connection-revoked keys fail closed.
4. Rotation leaves exactly one active key and the old key fails immediately.
5. Secret bytes never appear in database dumps, API responses, logs, traces, or error messages.
6. Vault timeout or denial rejects the request without fallback.
7. Replayed nonces fail under concurrent requests and across process restarts.
8. Oversized bodies, unsupported content types, invalid schemas, stale timestamps, and invalid HMACs fail closed.
9. Revocation racing an in-flight request has a documented, tested linearization point.
10. No order, account snapshot, wallet, or balance mutation capability is reachable through the bridge.

## Current implementation boundary

The repository currently contains HMAC verification and persistent nonce-consumption primitives, plus participant-scoped heartbeat/quote evidence persistence. It does not yet contain a managed vault adapter, key metadata/provisioning/rotation/revocation service, authenticated HTTP ingestion endpoint, rate limiting, append-only security audit stream, or MT5 bridge client. No broker is connected or verified. Paper trading remains the only trading mode; live order routing remains disabled.
