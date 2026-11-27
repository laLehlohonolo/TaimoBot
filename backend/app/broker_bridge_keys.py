"""Non-secret metadata lifecycle for per-connection broker bridge keys.

Provisioned activations require a live fencing token. Direct metadata activation
remains available only for the existing internal/manual rotation workflow.
"""
import re
import uuid
from datetime import datetime, timezone

from .database import get_connection


_SECRET_REF_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{8,240}$")


class BridgeKeyMetadataError(ValueError):
    """Raised when key metadata cannot be safely created or changed."""


class BridgeKeyActivationLeaseError(BridgeKeyMetadataError):
    """Raised when an activation worker no longer owns a live lease."""


def ensure_bridge_key_schema(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_bridge_keys (
            key_id TEXT PRIMARY KEY,
            connection_id TEXT NOT NULL,
            secret_ref TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'REVOKED')),
            rotation_sequence INTEGER NOT NULL CHECK (rotation_sequence >= 1),
            created_at TEXT NOT NULL,
            activated_at TEXT NOT NULL,
            revoked_at TEXT,
            created_by TEXT,
            FOREIGN KEY (connection_id)
                REFERENCES participant_broker_connections(connection_id)
                ON DELETE CASCADE,
            FOREIGN KEY (created_by) REFERENCES users(user_id)
                ON DELETE SET NULL
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_broker_bridge_one_active_key
        ON broker_bridge_keys(connection_id)
        WHERE status = 'ACTIVE'
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_broker_bridge_keys_connection_history
        ON broker_bridge_keys(connection_id, rotation_sequence)
        """
    )


def _validate_secret_ref(secret_ref):
    if (
        not isinstance(secret_ref, str)
        or not _SECRET_REF_PATTERN.fullmatch(secret_ref)
        or not secret_ref.startswith(("vault://", "secretmanager://"))
    ):
        raise BridgeKeyMetadataError("A valid opaque secret-manager reference is required.")
    lowered = secret_ref.casefold()
    if any(marker in lowered for marker in ("password=", "token=", "secret=")):
        raise BridgeKeyMetadataError("Secret material must never be supplied as metadata.")


def activate_bridge_key(
    connection_id, secret_ref, *, created_by=None, now=None,
    operation_id=None, activation_token=None,
):
    """Activate key metadata and, for provisioning, complete its operation atomically."""
    if not isinstance(connection_id, str) or not connection_id:
        raise BridgeKeyMetadataError("A connection ID is required.")
    _validate_secret_ref(secret_ref)
    if (operation_id is None) != (activation_token is None):
        raise BridgeKeyMetadataError("Both provisioning operation ID and activation token are required.")
    timestamp = now or datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise BridgeKeyMetadataError("A timezone-aware timestamp is required.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BridgeKeyMetadataError("A timezone-aware timestamp is required.")
    timestamp = parsed.astimezone(timezone.utc).isoformat()
    key_id = str(uuid.uuid4())

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if operation_id is not None:
            lease = connection.execute(
                """SELECT status, activation_token, lease_expires_at, secret_ref,
                          connection_id, created_by
                   FROM broker_bridge_provisioning_operations
                   WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()
            try:
                lease_expiry = datetime.fromisoformat(
                    lease["lease_expires_at"].replace("Z", "+00:00")
                ) if lease and lease["lease_expires_at"] else None
            except ValueError:
                lease_expiry = None
            if (
                lease is None or lease["status"] != "ACTIVATING"
                or lease["activation_token"] != activation_token
                or lease["connection_id"] != connection_id
                or lease["created_by"] != created_by
                or lease["secret_ref"] != secret_ref
                or lease_expiry is None or lease_expiry.tzinfo is None
                or lease_expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc)
            ):
                raise BridgeKeyActivationLeaseError(
                    "Activation lease is no longer owned by this worker."
                )
        owner = connection.execute(
            "SELECT state FROM participant_broker_connections WHERE connection_id = ?",
            (connection_id,),
        ).fetchone()
        if owner is None or owner["state"] != "REQUESTED":
            raise BridgeKeyMetadataError("Only an active requested connection can receive a key.")
        if not isinstance(created_by, str) or not created_by:
            raise BridgeKeyMetadataError("A verified administrator must provision bridge keys.")
        actor = connection.execute(
            "SELECT role, email_verified FROM users WHERE user_id = ?",
            (created_by,),
        ).fetchone()
        if (
            actor is None
            or actor["role"] != "ADMIN"
            or int(actor["email_verified"]) != 1
        ):
            raise BridgeKeyMetadataError(
                "Only a verified administrator may provision bridge keys."
            )
        latest = connection.execute(
            """
            SELECT COALESCE(MAX(rotation_sequence), 0) AS sequence
            FROM broker_bridge_keys WHERE connection_id = ?
            """,
            (connection_id,),
        ).fetchone()
        sequence = latest["sequence"] + 1
        connection.execute(
            """
            UPDATE broker_bridge_keys
            SET status = 'REVOKED', revoked_at = ?
            WHERE connection_id = ? AND status = 'ACTIVE'
            """,
            (timestamp, connection_id),
        )
        connection.execute(
            """
            INSERT INTO broker_bridge_keys (
                key_id, connection_id, secret_ref, status, rotation_sequence,
                created_at, activated_at, created_by
            ) VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, ?)
            """,
            (key_id, connection_id, secret_ref, sequence, timestamp, timestamp, created_by),
        )
        if operation_id is not None:
            completed = connection.execute(
                """UPDATE broker_bridge_provisioning_operations
                   SET status = 'ACTIVATED', key_id = ?, lease_expires_at = NULL,
                       activation_token = NULL, last_error_code = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE operation_id = ? AND status = 'ACTIVATING'
                     AND activation_token = ?""",
                (key_id, operation_id, activation_token),
            )
            if completed.rowcount != 1:
                raise BridgeKeyActivationLeaseError(
                    "Activation lease changed before metadata could be committed."
                )
    return {
        "key_id": key_id,
        "connection_id": connection_id,
        "status": "ACTIVE",
        "rotation_sequence": sequence,
        "created_at": timestamp,
        "activated_at": timestamp,
        "secret_material_returned": False,
    }


def revoke_bridge_key(connection_id, key_id, *, now=None):
    if not isinstance(connection_id, str) or not connection_id:
        raise BridgeKeyMetadataError("A connection ID is required.")
    if not isinstance(key_id, str) or not key_id:
        raise BridgeKeyMetadataError("A key ID is required.")
    timestamp = now or datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise BridgeKeyMetadataError("A timezone-aware timestamp is required.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BridgeKeyMetadataError("A timezone-aware timestamp is required.")
    timestamp = parsed.astimezone(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE broker_bridge_keys
            SET status = 'REVOKED', revoked_at = ?
            WHERE connection_id = ? AND key_id = ? AND status = 'ACTIVE'
            """,
            (timestamp, connection_id, key_id),
        )
    return cursor.rowcount == 1


def get_active_bridge_key_metadata(connection_id):
    """Return non-secret metadata only; secret_ref is intentionally excluded."""
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT k.key_id, k.connection_id, k.status, k.rotation_sequence,
                   k.created_at, k.activated_at, c.state AS connection_state
            FROM broker_bridge_keys k
            JOIN participant_broker_connections c
              ON c.connection_id = k.connection_id
            WHERE k.connection_id = ? AND k.status = 'ACTIVE'
            """,
            (connection_id,),
        ).fetchone()
    if row is None or row["connection_state"] != "REQUESTED":
        return None
    return dict(row)


def resolve_bridge_key_auth_context(key_id):
    """Resolve an active key to server-owned connection and vault metadata."""
    if not isinstance(key_id, str) or not key_id:
        return None
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT k.key_id, k.connection_id, k.secret_ref, k.status,
                   c.participant_user_id, c.state AS connection_state
            FROM broker_bridge_keys k
            JOIN participant_broker_connections c
              ON c.connection_id = k.connection_id
            WHERE k.key_id = ?
            """,
            (key_id,),
        ).fetchone()
    if row is None:
        return None
    if row["status"] != "ACTIVE" or row["connection_state"] != "REQUESTED":
        return None
    return {
        "key_id": row["key_id"],
        "connection_id": row["connection_id"],
        "participant_user_id": row["participant_user_id"],
        "secret_ref": row["secret_ref"],
    }
