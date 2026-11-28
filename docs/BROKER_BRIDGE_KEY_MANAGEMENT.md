# Broker Bridge Key Management Contract

Status: security design contract with a partial internal implementation. A production vault adapter, provisioning HTTP API, and authenticated bridge endpoint are not implemented.

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

The repository contains HMAC verification and persistent nonce-consumption primitives, participant-scoped heartbeat/quote evidence persistence, key metadata lifecycle helpers, and an internal vault-neutral provisioning orchestrator with fenced activation leases. It does not yet contain a managed vault adapter, provisioning HTTP route, authenticated HTTP ingestion endpoint, complete rate limiting, append-only security audit stream, or MT5 bridge client. No broker is connected or verified. Paper trading remains the only trading mode; live order routing remains disabled.

## Vault-neutral provisioning service (implemented)

`backend/app/broker_bridge_provisioning.py` defines a narrow `SecretManager` protocol and an internal orchestration boundary:

1. Ask the injected secret manager to generate and persist 256 bits of cryptographically random key material under the durable operation ID.
2. Require the secret manager to return the same opaque reference and preserve the exact same secret bytes when the same operation ID is retried after an ambiguous timeout. The adapter owns one-time generation; the orchestration layer never generates replacement bytes on retry.
3. Activate non-secret database metadata only after secret creation succeeds.
4. If metadata activation fails, attempt to delete the newly created secret.
5. If compensation also fails, return a generic operator-cleanup error without disclosing the secret reference or key material.

Provisioning preflights a persisted administrator whose email is verified and a REQUESTED connection before any vault write. Ordinary participants, unknown actors, paused/revoked connections and unknown connections fail closed. The metadata helper independently repeats authorization and connection-state checks transactionally, closing the time-of-check/time-of-use gap; a state change during vault creation triggers compensation.

**Not yet implemented:** a production vault adapter, vault access policy, secret retrieval/cache invalidation, provisioning HTTP route, audit event pipeline, or authenticated evidence-ingestion gateway. The included fake secret manager exists only in tests and is not a production secret store. Python cannot guarantee zeroization of immutable `bytes`; the generated value is kept within the short provisioning call and is never returned or logged.

The service does not enable broker connectivity or trading. Broker bridge evidence remains read-only and paper trading remains separate.


## Idempotent vault creation and ambiguous timeouts

The provisioning protocol now requires an idempotency key on each secret-creation call. The key is a UUID operation identifier, not secret material. A compliant vault adapter must generate the secret material once, atomically bind it to that operation ID, and guarantee that repeated create requests with the same idempotency key resolve to the same secret reference and the exact same secret bytes without creating duplicate secret objects. The orchestrator must not generate fresh bytes for a retry: after an ambiguous timeout it cannot know whether the original secret was committed, so new bytes could mismatch the already-created vault object.

The provisioning caller must durably create and persist the operation ID before making the vault request, then reuse that exact ID when retrying after a timeout, lost response, or process restart. The internal orchestration helper accepts this stable `operation_id` and passes it through as the vault idempotency key. Secret generation occurs inside the adapter's idempotent create operation, not in the retrying caller. If the caller generates a new ID on retry, the vault cannot correlate the attempt and duplicate/orphaned secrets remain possible. If an adapter cannot guarantee both reference and byte stability for a repeated operation ID, it is not compliant and must not be used for provisioning.

**Current boundary:** the durable provisioning-operation table and recovery orchestration are implemented in `broker_bridge_provisioning_store.py` and `broker_bridge_provisioning.py`, with regression coverage using a fake secret manager. A production vault adapter, adapter-side idempotency guarantees, operator cleanup/reconciliation workflow, and provisioning route are still absent. Therefore crash recovery is covered at the application orchestration layer but is not yet end-to-end against a real vault. Do not expose a production provisioning route until the production adapter, retry/reconciliation policy, and audited operator cleanup path are implemented and tested.


## Durable provisioning operation registry (implemented)

`backend/app/broker_bridge_provisioning_store.py` persists an operation record before the vault is called. The operation ID is a UUID and is bound immutably to the connection and verified administrator. Reusing it with a different actor or connection is rejected.

Persisted states:
- `PENDING`: vault result may be unknown; retry only with the same operation ID.
- `SECRET_CREATED`: opaque vault reference is stored; metadata activation can resume.
- `ACTIVATING`: one worker holds a five-minute database lease; expired leases can be reclaimed after a crash.
- `ACTIVATED`: key metadata activation is complete and the key ID is recorded.
- `CLEANUP_REQUIRED`: vault deletion failed; automatic retry is blocked pending operator reconciliation.
- `FAILED`: activation failed and the known vault secret was successfully deleted; the operation is terminal.

Activation claims now return unique UUID fencing tokens. Reclaiming an expired lease replaces the token, so a delayed worker cannot activate metadata or complete the operation with its stale token. The key metadata write validates the current token and unexpired lease inside the same SQLite transaction that inserts the key and changes the operation to `ACTIVATED`; both changes commit or roll back together. Failure cleanup first atomically moves the operation to a non-reclaimable `CLEANUP_REQUIRED` state while the worker still owns the lease, and only then attempts vault deletion. A worker that has lost ownership does not delete the secret or overwrite the newer worker's state. Legacy metadata committed before this atomic transition can be reconciled only after a new worker claims a current lease. Regression tests cover token replacement, stale-worker rejection, expired-lease rejection, and atomic completion. The service still recovers an ambiguous vault timeout by repeating the idempotent create call with the same operation ID.

The durable registry and orchestration are implemented and covered by tests using a fake secret manager. A production adapter must still guarantee idempotency at the vault side. The fake adapter does not prove that a cloud vault provides this guarantee. There is still no production vault integration, provisioning HTTP endpoint, operator cleanup workflow, or authenticated evidence-ingestion gateway.


## Hosting and production secret-manager decision (2026-11-28)

**Decision: AWS is the selected primary cloud platform.** The initial production target is Amazon ECS on AWS Fargate for the FastAPI backend, with AWS Secrets Manager as the managed secret store, AWS KMS customer-managed encryption keys where justified, and IAM task roles for workload identity. This is an architectural decision only; it does not authorize account creation, cloud provisioning, deployment, or enabling broker connectivity.

**Initial region: Africa (Cape Town), `af-south-1`.** AWS documentation currently lists Fargate, ECS, and Secrets Manager as available in this region. The region is opt-in for AWS accounts, so it must be explicitly enabled before use. Region choice remains subject to a measured latency, availability, service-quota, pricing, and data-residency review before production launch. Do not silently fall back to another region; any change requires an explicit documented review.

**Identity and access model:**
- The FastAPI workload receives a dedicated ECS task role. Grant only the exact Secrets Manager actions and secret ARNs required for its runtime function; do not use long-lived IAM user access keys in containers.
- Keep the ECS task execution role separate from the application task role. The execution role is for agent operations such as image pulls/log delivery and task-definition secret injection; the application task role is for application API calls.
- Separate provisioning, runtime retrieval, rotation, revocation, and operator-reconciliation permissions. The runtime service must not be able to create, rotate, or delete secrets unless a separately reviewed workflow requires it.
- Use a customer-managed KMS key with a narrowly scoped key policy when the threat model and operational ownership justify it. Restrict decrypt permissions to the required workload role and secret resources; log and alert on key-policy or grant changes.
- Store only opaque secret references and non-secret metadata in the application database. Never place broker passwords or bridge HMAC key bytes in environment variables, source control, logs, traces, API responses, or ChatGPT messages.
- Preserve the existing provider-neutral `SecretManager` and `SecretReader` interfaces. Implement AWS adapters behind these interfaces; do not spread AWS SDK calls through domain logic.
- The adapter must prove durable idempotency for secret creation, exact-byte stability after ambiguous timeouts, least-privilege retrieval, rotation/revocation behavior, and safe cleanup/reconciliation before any production route is enabled.

**Delivery sequence / gates:**
1. Add AWS adapter contract tests using fakes and local deterministic tests; do not claim these prove AWS behavior.
2. Implement and test AWS Secrets Manager adapters with workload identity and resource-scoped IAM policies in a non-production AWS account.
3. Verify real AWS idempotency/retry semantics, KMS policy behavior, audit logging, rotation, revocation, deletion/compensation, outage handling, and operator cleanup under failure injection.
4. Complete deployment threat model, regional latency/cost/quota review, privacy/data-residency review, backup/recovery and incident-response runbooks.
5. Only after those gates, consider a production provisioning route and read-only ingestion gateway behind explicit operator approval.

**Current status:** AWS is selected as the target, but no AWS account, infrastructure, IAM role, KMS key, Secrets Manager secret, production adapter, or deployment has been created. No broker is connected; no participant credentials have been collected; paper trading remains the only trading mode and live order routing remains disabled.
