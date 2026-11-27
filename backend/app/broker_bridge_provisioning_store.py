"""Durable state for broker bridge key provisioning operations."""
from .database import get_connection


STATES = ("PENDING", "SECRET_CREATED", "ACTIVATED", "CLEANUP_REQUIRED", "FAILED")


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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_error_code TEXT,
                FOREIGN KEY (connection_id)
                    REFERENCES participant_broker_connections(connection_id),
                FOREIGN KEY (created_by) REFERENCES users(user_id)
            )
        """)


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
                   updated_at = CURRENT_TIMESTAMP
               WHERE operation_id = ?""",
            (status, secret_ref, key_id, error_code, operation_id),
        )


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
