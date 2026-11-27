"""Durable state for broker bridge key provisioning operations."""
from datetime import datetime, timedelta, timezone

from .database import get_connection


STATES = ("PENDING", "SECRET_CREATED", "ACTIVATING", "ACTIVATED", "CLEANUP_REQUIRED", "FAILED")


def ensure_schema():
    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS broker_bridge_provisioning_operations (
                operation_id TEXT PRIMARY KEY,
                connection_id TEXT NOT NULL,
                created_by TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'PENDING', 'SECRET_CREATED', 'ACTIVATED',
                    'CLEANUP_REQUIRED', 'FAILED'
                )),
                secret_ref TEXT UNIQUE,
                key_id TEXT,
                lease_expires_at TEXT,
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
    if status not in STATES:
        raise ValueError("Invalid provisioning operation state.")
    with get_connection() as connection:
        connection.execute(
            """UPDATE broker_bridge_provisioning_operations
               SET status = ?, secret_ref = COALESCE(?, secret_ref),
                   key_id = COALESCE(?, key_id), last_error_code = ?,
                   lease_expires_at = CASE WHEN ? = 'ACTIVATING' THEN lease_expires_at ELSE NULL END,
                   updated_at = CURRENT_TIMESTAMP
               WHERE operation_id = ?""",
            (status, secret_ref, key_id, error_code, status, operation_id),
        )



def claim_activation(operation_id, *, lease_minutes=5):
    """Atomically claim activation; expired claims can be recovered after crashes."""
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(minutes=lease_minutes)).isoformat()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT status, lease_expires_at
               FROM broker_bridge_provisioning_operations
               WHERE operation_id = ?""",
            (operation_id,),
        ).fetchone()
        if row is None:
            return False
        reclaimable = row["status"] == "SECRET_CREATED"
        if row["status"] == "ACTIVATING" and row["lease_expires_at"]:
            try:
                prior_expiry = datetime.fromisoformat(row["lease_expires_at"])
                reclaimable = prior_expiry <= now
            except ValueError:
                reclaimable = True
        if not reclaimable:
            return False
        connection.execute(
            """UPDATE broker_bridge_provisioning_operations
               SET status = 'ACTIVATING', lease_expires_at = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE operation_id = ?""",
            (expires, operation_id),
        )
    return True

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
