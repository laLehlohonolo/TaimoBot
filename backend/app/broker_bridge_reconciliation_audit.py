"""Append-only, redacted operator audit for broker bridge reconciliation.

This module records explicit operator decisions only. It does not perform
cleanup, contact a vault, or change provisioning/connection/key state.
"""
import re
import uuid

from .database import get_connection
from .broker_bridge_reconciliation import _verified_admin


_ALLOWED_ACTIONS = {
    "INSPECT", "DRY_RUN", "OPERATOR_REVIEW", "ESCALATE",
}
_ALLOWED_OUTCOMES = {"RECORDED", "DENIED", "REQUIRES_FOLLOW_UP"}
_SENSITIVE_REASON_PATTERNS = (
    re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(
        r"\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|"
        r"client[_ -]?secret)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),
)


class ReconciliationAuditError(RuntimeError):
    """Raised when a reconciliation audit event is invalid or unauthorized."""


def _redact_reason(reason):
    if not isinstance(reason, str):
        raise ReconciliationAuditError("A reason is required.")
    reason = " ".join(reason.split())
    if not reason or len(reason) > 300:
        raise ReconciliationAuditError("Reason must contain 1 to 300 characters.")
    for pattern in _SENSITIVE_REASON_PATTERNS:
        reason = pattern.sub("[REDACTED]", reason)
    return reason


def ensure_schema():
    """Create the immutable audit ledger and its database-enforced guards."""
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS broker_bridge_reconciliation_audit (
                event_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                action TEXT NOT NULL CHECK (
                    action IN ('INSPECT', 'DRY_RUN', 'OPERATOR_REVIEW', 'ESCALATE')
                ),
                classification TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (
                    outcome IN ('RECORDED', 'DENIED', 'REQUIRES_FOLLOW_UP')
                ),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (operation_id)
                    REFERENCES broker_bridge_provisioning_operations(operation_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (actor_user_id)
                    REFERENCES users(user_id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_broker_bridge_reconciliation_audit_operation
                ON broker_bridge_reconciliation_audit(operation_id, created_at);

            CREATE TRIGGER IF NOT EXISTS prevent_broker_bridge_reconciliation_audit_update
            BEFORE UPDATE ON broker_bridge_reconciliation_audit
            BEGIN
                SELECT RAISE(ABORT, 'Broker bridge reconciliation audit is append-only.');
            END;

            CREATE TRIGGER IF NOT EXISTS prevent_broker_bridge_reconciliation_audit_delete
            BEFORE DELETE ON broker_bridge_reconciliation_audit
            BEGIN
                SELECT RAISE(ABORT, 'Broker bridge reconciliation audit is append-only.');
            END;
            """
        )


def append_event(
    *,
    actor_id,
    operation_id,
    reason,
    action,
    classification,
    outcome="RECORDED",
):
    """Append one redacted, authorized operator decision; never mutate operations."""
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
        raise ReconciliationAuditError("A valid operation ID is required.")
    if action not in _ALLOWED_ACTIONS:
        raise ReconciliationAuditError("Unsupported reconciliation audit action.")
    if outcome not in _ALLOWED_OUTCOMES:
        raise ReconciliationAuditError("Unsupported reconciliation audit outcome.")
    if (
        not isinstance(classification, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", classification)
    ):
        raise ReconciliationAuditError("A valid classification is required.")
    safe_reason = _redact_reason(reason)
    event_id = str(uuid.uuid4())

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not _verified_admin(connection, actor_id):
            raise ReconciliationAuditError("A verified administrator is required.")
        operation = connection.execute(
            "SELECT operation_id FROM broker_bridge_provisioning_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise ReconciliationAuditError("Provisioning operation was not found.")
        connection.execute(
            """
            INSERT INTO broker_bridge_reconciliation_audit (
                event_id, operation_id, actor_user_id, reason,
                action, classification, outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, operation_id, actor_id, safe_reason,
                action, classification, outcome,
            ),
        )
    return {
        "event_id": event_id,
        "operation_id": operation_id,
        "actor_user_id": actor_id,
        "reason": safe_reason,
        "action": action,
        "classification": classification,
        "outcome": outcome,
        "secret_material_exposed": False,
    }
