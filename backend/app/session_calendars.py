"""Fail-closed, versioned instrument session-calendar validation.

No calendars are pre-populated: trading hours must come from a verified,
instrument-specific broker/provider source. Structural validation is not
source verification or administrator approval and never enables trading.
"""
from datetime import date, datetime
from types import MappingProxyType
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .instruments import get_instrument


# Deliberately empty until a source has been independently verified and
# approved. Do not insert generic FX, metals, or synthetic-index schedules.
SESSION_CALENDARS = MappingProxyType({})


def _aware_datetime(value, field):
    if not isinstance(value, str):
        raise ValueError(field + " must be an ISO-8601 timestamp with timezone.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(field + " must be a valid ISO-8601 timestamp.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(field + " must include a timezone.")
    return parsed.isoformat()


def _iso_date(value, field):
    if not isinstance(value, str):
        raise ValueError(field + " must be an ISO date.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(field + " must be a valid ISO date.")
    if parsed.isoformat() != value:
        raise ValueError(field + " must use YYYY-MM-DD.")
    return parsed


def _clock_minutes(value, field):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError(field + " must use HH:MM.")
    try:
        hour, minute = int(value[:2]), int(value[3:])
    except ValueError:
        raise ValueError(field + " must use HH:MM.")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(field + " must be a valid 24-hour time.")
    return hour * 60 + minute


def validate_session_calendar_record(record):
    """Validate calendar shape; output always remains pending verification."""
    if not isinstance(record, dict):
        raise ValueError("Calendar record must be an object.")

    required = (
        "provider", "symbol", "timezone", "weekly_sessions", "holidays",
        "maintenance_windows", "effective_from", "effective_to", "source_url",
        "retrieved_at", "reviewed_by", "version",
    )
    missing = [field for field in required if field not in record]
    if missing:
        raise ValueError("Missing calendar fields: " + ", ".join(missing))

    provider = record["provider"]
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a non-empty identifier.")
    symbol = get_instrument(record["symbol"])["symbol"]

    timezone_name = record["timezone"]
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError("timezone must be an IANA timezone name.")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezone must be a valid IANA timezone name.")

    start_date = _iso_date(record["effective_from"], "effective_from")
    end_date = _iso_date(record["effective_to"], "effective_to")
    if end_date < start_date:
        raise ValueError("effective_to cannot precede effective_from.")

    version = record["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("version must be a positive integer.")

    source_url = record["source_url"]
    parsed_url = urlparse(source_url) if isinstance(source_url, str) else None
    if not parsed_url or parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError("source_url must be an HTTPS reference.")

    retrieved_at = _aware_datetime(record["retrieved_at"], "retrieved_at")
    reviewed_by = record["reviewed_by"]
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise ValueError("reviewed_by must identify the intended reviewer.")

    weekly = record["weekly_sessions"]
    if not isinstance(weekly, list):
        raise ValueError("weekly_sessions must be a list.")
    normalized_weekly = []
    occupied = {day: [] for day in range(7)}
    for index, session in enumerate(weekly):
        if not isinstance(session, dict):
            raise ValueError("Each weekly session must be an object.")
        day = session.get("weekday")
        if isinstance(day, bool) or not isinstance(day, int) or day not in range(7):
            raise ValueError("weekday must be an integer from 0 (Monday) to 6 (Sunday).")
        start = _clock_minutes(session.get("start"), "weekly_sessions.start")
        end = _clock_minutes(session.get("end"), "weekly_sessions.end")
        if start >= end:
            raise ValueError("Weekly sessions must be same-day intervals with start before end.")
        for prior_start, prior_end in occupied[day]:
            if start < prior_end and prior_start < end:
                raise ValueError("Weekly sessions cannot overlap on the same weekday.")
        occupied[day].append((start, end))
        normalized_weekly.append({
            "weekday": day, "start": session["start"], "end": session["end"],
        })

    holidays = record["holidays"]
    if not isinstance(holidays, list):
        raise ValueError("holidays must be a list of ISO dates.")
    normalized_holidays = sorted({
        _iso_date(item, "holiday").isoformat() for item in holidays
    })

    maintenance = record["maintenance_windows"]
    if not isinstance(maintenance, list):
        raise ValueError("maintenance_windows must be a list.")
    normalized_maintenance = []
    for window in maintenance:
        if not isinstance(window, dict):
            raise ValueError("Each maintenance window must be an object.")
        start = _aware_datetime(window.get("start"), "maintenance start")
        end = _aware_datetime(window.get("end"), "maintenance end")
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        if end_dt <= start_dt:
            raise ValueError("Maintenance window end must follow its start.")
        normalized_maintenance.append({"start": start, "end": end})

    return {
        "provider": provider.strip(),
        "symbol": symbol,
        "timezone": timezone_name,
        "weekly_sessions": normalized_weekly,
        "holidays": normalized_holidays,
        "maintenance_windows": normalized_maintenance,
        "effective_from": start_date.isoformat(),
        "effective_to": end_date.isoformat(),
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "reviewed_by": reviewed_by.strip(),
        "version": version,
        "status": "PENDING_SOURCE_VERIFICATION",
        "source_verified": False,
        "admin_reviewed": False,
        "session_calendar_applied": False,
        "session_adjusted_coverage_available": False,
        "backtest_eligible": False,
        "execution_eligible": False,
    }


def assess_session_calendar_readiness(symbol, calendar=None):
    """Only an explicitly verified and admin-reviewed record can be ready."""
    normalized_symbol = get_instrument(symbol)["symbol"]
    candidate = calendar if calendar is not None else SESSION_CALENDARS.get(normalized_symbol)
    if not isinstance(candidate, dict):
        return {
            "symbol": normalized_symbol,
            "status": "NOT_CONFIGURED",
            "session_calendar_id": None,
            "session_adjusted_coverage_available": False,
            "backtest_eligible": False,
            "execution_eligible": False,
        }

    ready = (
        candidate.get("symbol") == normalized_symbol
        and candidate.get("status") == "VERIFIED"
        and candidate.get("source_verified") is True
        and candidate.get("admin_reviewed") is True
        and isinstance(candidate.get("version"), int)
        and not isinstance(candidate.get("version"), bool)
        and candidate.get("version", 0) > 0
    )
    return {
        "symbol": normalized_symbol,
        "status": "READY" if ready else "PENDING_VERIFICATION",
        "session_calendar_id": (
            candidate.get("calendar_id") if ready else None
        ),
        "session_adjusted_coverage_available": ready,
        "backtest_eligible": False,
        "execution_eligible": False,
    }
