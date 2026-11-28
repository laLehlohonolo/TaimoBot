""""Rate-limited, audited HTTP boundary for read-only broker evidence.

The router is deliberately NOT mounted by the application yet. Deployment must
supply a least-privilege SecretReader and verify proxy-level abuse controls.
Only authenticated requests are written to the durable participant audit log.
"""
import os
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from .broker_bridge_ingestion import (
    BridgeIngestionRateLimitError,
    BridgeIngestionRequestError,
    MAX_EVIDENCE_BODY_BYTES,
    ingest_authenticated_bridge_evidence,
)
from .broker_bridge_evidence import BridgeEvidenceError
from .broker_bridge_keys import resolve_bridge_key_auth_context
from .broker_bridge_security import BridgeAuthenticationError
from .database import get_connection


PARTICIPANT_CAPACITY = 120
CONNECTION_CAPACITY = 30
BUCKET_WINDOW_SECONDS = 60
router = APIRouter()


def ensure_bridge_ingestion_gateway_schema(connection):
    """Create durable token buckets and an immutable, privacy-anonymisable audit log."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_bridge_ingestion_rate_limits (
            bucket_key TEXT PRIMARY KEY,
            bucket_scope TEXT NOT NULL CHECK (bucket_scope IN ('PARTICIPANT', 'CONNECTION')),
            participant_user_id TEXT NOT NULL,
            connection_id TEXT,
            tokens REAL NOT NULL CHECK (tokens >= 0),
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (participant_user_id) REFERENCES users(user_id) ON DELETE CASCADE,
            FOREIGN KEY (connection_id)
                REFERENCES participant_broker_connections(connection_id) ON DELETE SET NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bridge_ingestion_rate_owner
        ON broker_bridge_ingestion_rate_limits(participant_user_id, connection_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_bridge_ingestion_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            request_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN ('ATTEMPT', 'RESULT')),
            occurred_at TEXT NOT NULL,
            participant_user_id TEXT,
            connection_id TEXT,
            key_id TEXT,
            http_status INTEGER NOT NULL,
            reason_code TEXT NOT NULL,
            body_bytes INTEGER NOT NULL CHECK (body_bytes >= 0),
            FOREIGN KEY (participant_user_id) REFERENCES users(user_id) ON DELETE SET NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bridge_ingestion_audit_request
        ON broker_bridge_ingestion_audit(request_id, audit_id)
        """
    )
    # SQLite implements ON DELETE SET NULL as an UPDATE. Permit exactly that
    # privacy-preserving FK action while rejecting all edits to audit evidence.
    connection.execute("DROP TRIGGER IF EXISTS trg_bridge_ingestion_audit_no_update")
    connection.execute(
        """
        CREATE TRIGGER trg_bridge_ingestion_audit_no_update
        BEFORE UPDATE ON broker_bridge_ingestion_audit
        WHEN NOT (
            OLD.participant_user_id IS NOT NULL
            AND NEW.participant_user_id IS NULL
            AND NEW.audit_id IS OLD.audit_id
            AND NEW.event_id IS OLD.event_id
            AND NEW.request_id IS OLD.request_id
            AND NEW.event_type IS OLD.event_type
            AND NEW.occurred_at IS OLD.occurred_at
            AND NEW.connection_id IS OLD.connection_id
            AND NEW.key_id IS OLD.key_id
            AND NEW.http_status IS OLD.http_status
            AND NEW.reason_code IS OLD.reason_code
            AND NEW.body_bytes IS OLD.body_bytes
        )
        BEGIN
            SELECT RAISE(ABORT, 'Broker ingestion audit is immutable except participant anonymisation.');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_bridge_ingestion_audit_no_delete
        BEFORE DELETE ON broker_bridge_ingestion_audit
        BEGIN SELECT RAISE(ABORT, 'Broker ingestion audit is append-only.'); END
        """
    )


def append_bridge_ingestion_audit(
    *, request_id, event_type, participant_user_id=None, connection_id=None,
    key_id=None, http_status, reason_code, body_bytes=0, now=None,
):
    """Append allow-listed metadata only; never persist body, signature, nonce or secrets."""
    allowed_events = {"ATTEMPT", "RESULT"}
    allowed_reasons = {
        "REQUEST_RECEIVED", "ACCEPTED", "DUPLICATE_IGNORED",
        "CONTENT_TYPE_REJECTED", "BODY_TOO_LARGE", "BODY_INVALID",
        "AUTHENTICATION_REJECTED", "KEY_UNAVAILABLE", "IDENTITY_REJECTED",
        "RATE_LIMITED", "INGRESS_DISABLED", "INTERNAL_FAILURE",
    }
    if event_type not in allowed_events or reason_code not in allowed_reasons:
        raise ValueError("Unsupported broker-ingestion audit classification.")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 80:
        raise ValueError("A bounded request ID is required.")
    if isinstance(body_bytes, bool) or not isinstance(body_bytes, int) or body_bytes < 0:
        raise ValueError("body_bytes must be a non-negative integer.")
    occurred_at = now or datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO broker_bridge_ingestion_audit (
                event_id, request_id, event_type, occurred_at,
                participant_user_id, connection_id, key_id,
                http_status, reason_code, body_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), request_id, event_type, occurred_at,
                participant_user_id, connection_id, key_id,
                int(http_status), reason_code, body_bytes,
            ),
        )


def consume_bridge_ingestion_rate_limit(
    participant_user_id, connection_id, *, now=None,
    participant_capacity=PARTICIPANT_CAPACITY,
    connection_capacity=CONNECTION_CAPACITY,
    window_seconds=BUCKET_WINDOW_SECONDS,
):
    """Atomically consume participant-wide and per-connection token buckets."""
    if not participant_user_id or not connection_id:
        raise ValueError("Server-resolved participant and connection are required.")
    if min(participant_capacity, connection_capacity, window_seconds) <= 0:
        raise ValueError("Rate-limit capacities and window must be positive.")
    current = int(time.time() if now is None else now)
    specifications = (
        ("PARTICIPANT", f"participant:{participant_user_id}",
         participant_capacity, participant_capacity / window_seconds),
        ("CONNECTION", f"connection:{connection_id}",
         connection_capacity, connection_capacity / window_seconds),
    )
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        states = []
        for scope, bucket_key, capacity, refill_rate in specifications:
            row = connection.execute(
                "SELECT tokens, updated_at FROM broker_bridge_ingestion_rate_limits "
                "WHERE bucket_key = ?", (bucket_key,),
            ).fetchone()
            if row is None:
                tokens = float(capacity)
            else:
                elapsed = max(0, current - int(row["updated_at"]))
                tokens = min(float(capacity), float(row["tokens"]) + elapsed * refill_rate)
            states.append((scope, bucket_key, capacity, tokens, refill_rate))

        limited = [state for state in states if state[3] < 1.0]
        for scope, bucket_key, _capacity, tokens, _refill_rate in states:
            connection.execute(
                """
                INSERT INTO broker_bridge_ingestion_rate_limits (
                    bucket_key, bucket_scope, participant_user_id, connection_id,
                    tokens, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(bucket_key) DO UPDATE SET
                    tokens = excluded.tokens, updated_at = excluded.updated_at
                """,
                (bucket_key, scope, participant_user_id,
                 connection_id if scope == "CONNECTION" else None, tokens, current),
            )
        if limited:
            retry_after = max(
                1,
                max(int((1.0 - state[3]) / state[4]) + 1 for state in limited),
            )
            return {"allowed": False, "scope": limited[0][0], "retry_after": retry_after}

        for _scope, bucket_key, _capacity, tokens, _refill_rate in states:
            connection.execute(
                "UPDATE broker_bridge_ingestion_rate_limits SET tokens = ? WHERE bucket_key = ?",
                (tokens - 1.0, bucket_key),
            )
    return {"allowed": True, "scope": None, "retry_after": 0}


def _http_rejection(status, detail, *, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    raise HTTPException(status_code=status, detail=detail, headers=headers)


async def _read_bounded_body(request):
    # This is a memory bound, not a substitute for proxy-level pre-auth limits.
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_EVIDENCE_BODY_BYTES:
            _http_rejection(413, "Evidence body exceeds the allowed size.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/internal/broker-bridge/connections/{connection_id}/evidence")
async def ingest_bridge_evidence_http(connection_id: str, request: Request):
    """Authenticated read-only evidence ingress; router is not mounted by default."""
    request_id = str(uuid.uuid4())
    key_id = request.headers.get("x-taimo-key-id")
    if os.getenv("TAIMOBOT_BROKER_INGESTION_HTTP_ENABLED", "").lower() != "true":
        _http_rejection(404, "Broker evidence ingress is disabled.")

    try:
        content_type = request.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            _http_rejection(415, "A JSON content type is required.")
        length_header = request.headers.get("content-length")
        if length_header is not None:
            if not length_header.isdigit():
                _http_rejection(400, "Invalid request length.")
            if int(length_header) > MAX_EVIDENCE_BODY_BYTES:
                _http_rejection(413, "Evidence body exceeds the allowed size.")

        timestamp = request.headers.get("x-taimo-timestamp")
        nonce = request.headers.get("x-taimo-nonce")
        signature = request.headers.get("x-taimo-signature")
        if not all((connection_id, key_id, timestamp, nonce, signature)):
            _http_rejection(401, "Bridge authentication is required.")

        context = resolve_bridge_key_auth_context(key_id)
        if context is None or context.get("connection_id") != connection_id:
            _http_rejection(401, "Bridge identity is invalid.")
        participant_user_id = context["participant_user_id"]

        reader = getattr(request.app.state, "broker_bridge_secret_reader", None)
        if reader is None:
            _http_rejection(503, "Bridge authentication is unavailable.")

        raw_body = await _read_bounded_body(request)
        body_bytes = len(raw_body)

        def after_authenticate(_authentication, _secret_context):
            # HMAC, persistent nonce, active-key status, and connection state have
            # passed. Only now may this request consume quota or create audit rows.
            append_bridge_ingestion_audit(
                request_id=request_id, event_type="ATTEMPT",
                participant_user_id=participant_user_id, connection_id=connection_id,
                key_id=key_id, http_status=0, reason_code="REQUEST_RECEIVED",
                body_bytes=body_bytes,
            )
            limit = consume_bridge_ingestion_rate_limit(
                participant_user_id, connection_id,
            )
            if not limit["allowed"]:
                append_bridge_ingestion_audit(
                    request_id=request_id, event_type="RESULT",
                    participant_user_id=participant_user_id, connection_id=connection_id,
                    key_id=key_id, http_status=429, reason_code="RATE_LIMITED",
                    body_bytes=body_bytes,
                )
                raise BridgeIngestionRateLimitError(
                    limit["retry_after"], limit["scope"],
                )

        result = ingest_authenticated_bridge_evidence(
            connection_id=connection_id, key_id=key_id,
            timestamp=timestamp, nonce=nonce, signature=signature,
            raw_body=raw_body, content_type=content_type,
            secret_reader=reader, after_authenticate=after_authenticate,
        )
        reason = "DUPLICATE_IGNORED" if result["status"] == "DUPLICATE_IGNORED" else "ACCEPTED"
        append_bridge_ingestion_audit(
            request_id=request_id, event_type="RESULT",
            participant_user_id=participant_user_id, connection_id=connection_id,
            key_id=key_id, http_status=200, reason_code=reason, body_bytes=body_bytes,
        )
        return result
    except BridgeIngestionRateLimitError as error:
        _http_rejection(429, "Bridge evidence rate limit exceeded.", retry_after=error.retry_after)
    except HTTPException:
        # Pre-authentication failures are deliberately not written to durable audit.
        raise
    except BridgeAuthenticationError:
        _http_rejection(401, "Bridge authentication failed.")
    except (BridgeIngestionRequestError, BridgeEvidenceError) as error:
        message = str(error).lower()
        status = (
            503 if "unavailable" in message else
            413 if "size" in message else
            415 if "content type" in message else 400
        )
        _http_rejection(status, "Evidence request rejected.")
    except Exception:
        # Authenticated audit failures abort before evidence persistence.
        _http_rejection(503, "Broker evidence ingress is temporarily unavailable.")
