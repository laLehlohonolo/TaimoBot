
import threading

_lock = threading.Lock()
_emergency_stop = False


def activate_emergency_stop():
    global _emergency_stop

    with _lock:
        _emergency_stop = True

    return {
        "emergency_stop": True,
        "new_orders_allowed": False,
        "message": "Emergency stop activated."
    }


def deactivate_emergency_stop():
    global _emergency_stop

    with _lock:
        _emergency_stop = False

    return {
        "emergency_stop": False,
        "new_orders_allowed": True,
        "message": "Emergency stop deactivated."
    }


def is_emergency_stop_active():
    with _lock:
        return _emergency_stop


def require_trading_enabled():
    if is_emergency_stop_active():
        raise RuntimeError(
            "Trading blocked: emergency stop is active."
        )
