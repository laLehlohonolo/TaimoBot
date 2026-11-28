"""Read-only operator inspection for broker bridge provisioning reconciliation.

This first reconciliation slice deliberately has no vault adapter and no
mutating cleanup function. It exposes non-secret evidence only; destructive
cleanup must be added behind a separately reviewed fencing/audit workflow.
"""
from .database import get_connection


class ReconciliationInspectionError(RuntimeError):
    """Raised when an authorized, read-only reconciliation inspection is unsafe."""


def _verified_admin(connection, actor_id):
    if not isinstance(actor_id, str) or not actor_id:
        return False
    actor = connection.execute(
        "SELECT role, email_verified FROM users WHERE user_id = ?",
        (actor_id,),
    ).fetchone()
    return (
        actor is not None
        and actor["role"] == "ADMIN"
        and int(actor["email_verified"]) == 1
    )


def list_provisioning_operations(*, actor_id, limit=100):
    """Return a bounded, read-only inventory without secret references/material."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ReconciliationInspectionError("A valid inventory limit is required.")
    with get_connection() as connection:
        connection.execute("BEGIN")
        if not _verified_admin(connection, actor_id):
            raise ReconciliationInspectionError("A verified administrator is required.")
        rows = connection.execute(
            """SELECT operation_id, connection_id, created_by, status,
                      lease_expires_at, created_at, updated_at, last_error_code
               FROM broker_bridge_provisioning_operations
               ORDER BY updated_at ASC, operation_id ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def preflight_provisioning_operation(*, actor_id, operation_id):
    """Classify one operation using a consistent read-only database snapshot.

    This function never reads or returns secret_ref, never calls a vault, and
    never changes operation, key, connection, or audit state.
    """
    if not isinstance(operation_id, str) or not operation_id:
        raise ReconciliationInspectionError("A valid provisioning operation ID is required.")
    with get_connection() as connection:
        connection.execute("BEGIN")
        if not _verified_admin(connection, actor_id):
            raise ReconciliationInspectionError("A verified administrator is required.")
        operation = connection.execute(
            """SELECT operation_id, connection_id, created_by, status,
                      lease_expires_at, created_at, updated_at, last_error_code,
                      secret_ref
               FROM broker_bridge_provisioning_operations
               WHERE operation_id = ?""",
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise ReconciliationInspectionError("Provisioning operation was not found.")
        broker_connection = connection.execute(
            "SELECT state FROM participant_broker_connections WHERE connection_id = ?",
            (operation["connection_id"],),
        ).fetchone()
        # Check every key status. A revoked key still proves that this secret
        # has already been committed to key metadata and must not be deleted.
        key_reference = connection.execute(
            "SELECT key_id, status FROM broker_bridge_keys WHERE secret_ref = ?",
            (operation["secret_ref"],),
        ).fetchone() if operation["secret_ref"] else None

    status = operation["status"]
    if key_reference is not None:
        classification = "BLOCKED_KEY_METADATA_REFERENCE"
        proposed_action = "DO_NOT_DELETE"
        evidence = "A broker bridge key metadata row references this secret."
    elif status == "ACTIVATED":
        classification = "INCONSISTENT_ACTIVATED_WITHOUT_KEY"
        proposed_action = "ESCALATE_FOR_REVIEW"
        evidence = "Operation is marked activated but matching key metadata was not found."
    elif status == "ACTIVATING":
        classification = "REVIEW_ACTIVATION_LEASE"
        proposed_action = "INSPECT_LEASE_AND_RETRY_STATE"
        evidence = "Activation may still be owned by a worker; no cleanup is authorized."
    elif status in ("SECRET_CREATED", "CLEANUP_REQUIRED"):
        classification = "REQUIRES_OPERATOR_REVIEW"
        proposed_action = "DRY_RUN_ONLY"
        evidence = "Vault existence and deletion safety require an independently verified vault check."
    elif status == "PENDING":
        classification = "NO_SECRET_CREATION_RECORDED"
        proposed_action = "NO_ACTION"
        evidence = "No secret reference is recorded for this operation."
    else:
        classification = "TERMINAL_NO_ACTION"
        proposed_action = "NO_ACTION"
        evidence = "Operation is in a terminal state."
    return {
        "operation_id": operation["operation_id"],
        "connection_id": operation["connection_id"],
        "created_by": operation["created_by"],
        "status": status,
        "connection_state": broker_connection["state"] if broker_connection else None,
        "lease_expires_at": operation["lease_expires_at"],
        "created_at": operation["created_at"],
        "updated_at": operation["updated_at"],
        "last_error_code": operation["last_error_code"],
        "classification": classification,
        "proposed_action": proposed_action,
        "evidence": evidence,
        "secret_reference_exposed": False,
        "secret_material_exposed": False,
        "mutated": False,
        "vault_contacted": False,
    }
