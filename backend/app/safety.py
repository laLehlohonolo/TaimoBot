""""Database-backed global safety controls."""

from .database import get_connection
from .maintenance import is_maintenance_window
from .tssi import require_security_clear


def _set_emergency_stop(active):
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO system_safety_state (
                singleton_id, emergency_stop_active, updated_at
            )
            VALUES (1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(singleton_id) DO UPDATE SET
                emergency_stop_active = excluded.emergency_stop_active,
                updated_at = CURRENT_TIMESTAMP
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
    maintenance_active = is_maintenance_window()
    return {
        "emergency_stop": False,
        "maintenance_window_active": maintenance_active,
        "new_orders_allowed": not maintenance_active,
        "block_reason": "DAILY_MAINTENANCE" if maintenance_active else None,
        "message": (
            "Emergency stop deactivated. New entries remain blocked "
            "during the daily maintenance window."
            if maintenance_active
            else "Emergency stop deactivated."
        )
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
    # TSSI is an independent veto: neither Engine/Brain nor the ordinary
    # emergency-stop resume endpoint can clear this security interlock.
    require_security_clear()

    if is_emergency_stop_active():
        raise RuntimeError(
            "Trading blocked: emergency stop is active."
        )

    if is_maintenance_window():
        raise RuntimeError(
            "Trading blocked: daily maintenance window is active "
            "(23:00-00:00 Africa/Johannesburg)."
        )
