"""Database-backed global safety controls."""

from .database import get_connection


def _set_emergency_stop(active):
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE system_safety_state
            SET emergency_stop_active = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE singleton_id = 1
            """,
            (1 if active else 0,)
        )


def activate_emergency_stop():
    _set_emergency_stop(True)
    return {
        "emergency_stop": True,
        "new_orders_allowed": False,
        "message": "Emergency stop activated."
    }


def deactivate_emergency_stop():
    _set_emergency_stop(False)
    return {
        "emergency_stop": False,
        "new_orders_allowed": True,
        "message": "Emergency stop deactivated."
    }


def is_emergency_stop_active():
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT emergency_stop_active
            FROM system_safety_state
            WHERE singleton_id = 1
            """
        ).fetchone()
    return bool(row["emergency_stop_active"]) if row is not None else True


def require_trading_enabled():
    if is_emergency_stop_active():
        raise RuntimeError(
            "Trading blocked: emergency stop is active."
        )
