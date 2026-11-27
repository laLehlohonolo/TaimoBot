"""Durable state for broker bridge key provisioning operations."""
import uuid
from datetime import datetime, timedelta, timezone

from .database import get_connection


STATES = ("PENDING", "SECRET_CREATED", "ACTIVATING", "ACTIVATED", "CLEANUP_REQUIRED", "FAILED")


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def ensure_schema():
    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS broker_bridge_provisioning_operations (
                operation_id TEXT PRIMARY KEY,
                connection_id TEXT NOT NULL,
                created_by TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'PENDING', 'SECRET_CREATED', 'ACTIVATING', 'ACTIVATED',
                    'CLEANUP_REQUIRED', 'FAILED'
                )),
                secret_ref TEXT UNIQUE,
                key_id TEXT,
                lease_expires_at TEXT,
                activation_token TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_error_code TEXT,
                FOREIGN KEY (connection_id)
                    REFERENCES participant_broker_connections(connection_id),
                FOREIGN KEY (created_by) REFERENCES users(user_id)
            )
        """)
        columns = {
            row["name"] for row in connection.execute(
                "PRAGMA table_info(broker_bridge_provisioning_operations)"
            ).fetchall()
        }
        if "lease_expires_at" not in columns:
            connection.execute(
                "ALTER TABLE broker_bridge_provisioning_operations ADD COLUMN lease_expires_at TEXT"
            )
        if "activation_token" not in columns:
            connection.execute(
                "ALTER TABLE broker_bridge_provisioning_operations ADD COLUMN activation_token TEXT"
            )


def load_or_create(operation_id, connection_id, created_by):
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM broker_bridge_provisioning_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """INSERT INTO broker_bridge_provisioning_operations
                   (operation_id, connection_id, created_by, status)
                   VALUES (?, ?, ?, 'PENDING')""",
                (operation_id, connection_id, created_by),
            )
            row = connection.execute(
                "SELECT * FROM broker_bridge_provisioning_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        elif row["connection_id"] != connection_id or row["created_by"] != created_by:
            raise ValueError("Provisioning operation binding mismatch.")
    return dict(row)


def update(operation_id, status, *, secret_ref=None, key_id=None, error_code=None):
    """Update non-claimed operation state; never overwrite an active lease."""
    if status not in STATES:
        raise ValueError("Invalid provisioning operation state.")
    if status == "ACTIVATED":
        raise ValueError("Activation completion must be committed with key metadata.")
    with get_connection() as connection:
        cursor = connection.execute(
            """UPDATE broker_bridge_provisioning_operations
               SET status = ?, secret_ref = COALESCE(?, secret_ref),
                   key_id = COALESCE(?, key_id), last_error_code = ?,
                   lease_expires_at = NULL, activation_token = NULL,
                   updated_at = CURRENT_TIMESTAMP
               WHERE operation_id = ? AND status <> 'ACTIVATING'""",
            (status, secret_ref, key_id, error_code, operation_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Provisioning operation is missing or currently leased.")


def claim_activation(operation_id, *, lease_minutes=5):
    """Claim activation and return a unique fencing token; None means unavailable."""
    if isinstance(lease_minutes, bool) or not isinstance(lease_minutes, int) or lease_minutes <= 0:
        raise ValueError("Activation lease duration must be a positive integer.")
    now = _utc_now()
    expires = (now + timedelta(minutes=lease_minutes)).isoformat()
    token = str(uuid.uuid4())
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT status, lease_expires_at
               FROM broker_bridge_provisioning_operations
               WHERE operation_id = ?""",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        reclaimable = row["status"] == "SECRET_CREATED"
        if row["status"] == "ACTIVATING":
            prior_expiry = _parse_timestamp(row["lease_expires_at"])
            reclaimable = prior_expiry is None or prior_expiry <= now
        if not reclaimable:
            return None
        cursor = connection.execute(
            """UPDATE broker_bridge_provisioning_operations
               SET status = 'ACTIVATING', lease_expires_at = ?,
                   activation_token = ?, updated_at = CURRENT_TIMESTAMP
               WHERE operation_id = ?""",
            (expires, token, operation_id),
        )
        if cursor.rowcount != 1:
            return None
    return token


def update_activation_owner(operation_id, activation_token, status, *, error_code=None):
    """Atomically reserve a terminal failure state for the current unexpired owner."""
    if status not in ("CLEANUP_REQUIRED", "FAILED"):
        raise ValueError("Invalid fenced activation failure state.")
    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT status, activation_token, lease_expires_at
               FROM broker_bridge_provisioning_operations WHERE operation_id = ?""",
            (operation_id,),
        ).fetchone()
        expiry = _parse_timestamp(row["lease_expires_at"]) if row else None
        if (
            row is None or row["status"] != "ACTIVATING"
            or row["activation_token"] != activation_token
            or expiry is None or expiry <= now
        ):
            return False
        cursor = connection.execute(
            """UPDATE broker_bridge_provisioning_operations
               SET status = ?, last_error_code = ?, lease_expires_at = NULL,
                   activation_token = NULL, updated_at = CURRENT_TIMESTAMP
               WHERE operation_id = ? AND status = 'ACTIVATING'
                 AND activation_token = ?""",
            (status, error_code, operation_id, activation_token),
        )
        return cursor.rowcount == 1


def complete_existing_activation(operation_id, connection_id, activation_token, key_id):
    """Reconcile legacy committed metadata only while holding the current lease."""
    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        operation = connection.execute(
            """SELECT status, activation_token, lease_expires_at, secret_ref
               FROM broker_bridge_provisioning_operations
               WHERE operation_id = ? AND connection_id = ?""",
            (operation_id, connection_id),
        ).fetchone()
        expiry = _parse_timestamp(operation["lease_expires_at"]) if operation else None
        if (
            operation is None or operation["status"] != "ACTIVATING"
            or operation["activation_token"] != activation_token
            or expiry is None or expiry <= now
        ):
            return False
        key = connection.execute(
            """SELECT key_id FROM broker_bridge_keys
               WHERE key_id = ? AND connection_id = ? AND secret_ref = ?""",
            (key_id, connection_id, operation["secret_ref"]),
        ).fetchone()
        if key is None:
            return False
        cursor = connection.execute(
            """UPDATE broker_bridge_provisioning_operations
               SET status = 'ACTIVATED', key_id = ?, lease_expires_at = NULL,
                   activation_token = NULL, last_error_code = NULL,
                   updated_at = CURRENT_TIMESTAMP
               WHERE operation_id = ? AND status = 'ACTIVATING'
                 AND activation_token = ?""",
            (key_id, operation_id, activation_token),
        )
        return cursor.rowcount == 1


def key_metadata(operation_id, connection_id):
    with get_connection() as connection:
        operation = connection.execute(
            """SELECT key_id FROM broker_bridge_provisioning_operations
               WHERE operation_id = ? AND connection_id = ? AND status = 'ACTIVATED'""",
            (operation_id, connection_id),
        ).fetchone()
        if operation is None or not operation["key_id"]:
            return None
        row = connection.execute(
            """SELECT key_id, connection_id, status, rotation_sequence,
                      created_at, activated_at
               FROM broker_bridge_keys WHERE key_id = ? AND connection_id = ?""",
            (operation["key_id"], connection_id),
        ).fetchone()
    if row is None:
        return None
    return {**dict(row), "secret_material_returned": False}
