"""Fail-closed, provider-neutral Market health evaluation.

This module evaluates server-supplied evidence only. It performs no network
requests, writes no observations, creates no signals, and cannot enable orders.
Provider trust configuration must come from server-controlled configuration.
"""
from datetime import datetime, timezone


MARKET_HEALTH_CONTRACT_VERSION = "market-health-v1"
MARKET_STATUSES = ("HEALTHY", "DEGRADED", "OFFLINE", "BLOCKED", "NOT_IMPLEMENTED")


def _parse_aware_timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field + " must be a timezone-aware ISO-8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(field + " must be a valid ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(field + " must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _non_negative_integer(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(field + " must be a non-negative integer.")
    return value


def _positive_integer(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(field + " must be a positive integer.")
    return value


def _result(status, reason, checked_at=None, latest_observation_at=None,
            age_seconds=None, provider_id=None, provider_version=None,
            evidence_id=None):
    return {
        "layer_id": "MARKET",
        "status": status,
        "reason": reason,
        "observed_at": checked_at.isoformat() if checked_at else None,
        "last_success_at": latest_observation_at.isoformat() if latest_observation_at else None,
        "age_seconds": age_seconds,
        "provider_id": provider_id,
        "provider_version": provider_version,
        "evidence_id": evidence_id,
        "contract_version": MARKET_HEALTH_CONTRACT_VERSION,
        "execution_eligible": False,
        "signals_generated": False,
        "orders_submitted": False,
    }


def evaluate_market_health(evidence=None, trusted_provider_id=None,
                           trusted_configuration=None, expected_instrument="XAUUSD", now=None):
    """Map one server-collected provider observation to a conservative status.

    trusted_provider_id is server-owned configuration, never request input.
    A Deriv synthetic-index observation cannot pass the XAUUSD instrument gate.
    """
    checked_now = now or datetime.now(timezone.utc)
    if not isinstance(checked_now, datetime) or checked_now.tzinfo is None or checked_now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime.")
    checked_now = checked_now.astimezone(timezone.utc)

    if evidence is None or not isinstance(evidence, dict) or not evidence.get("provider_id"):
        return _result("NOT_IMPLEMENTED", "No Market provider is registered.")

    provider_id = evidence.get("provider_id")
    provider_version = evidence.get("provider_version")
    instrument = evidence.get("instrument")
    symbol = evidence.get("canonical_symbol")
    if provider_id != trusted_provider_id or not isinstance(trusted_provider_id, str) or not trusted_provider_id:
        return _result("BLOCKED", "Provider identity is not present in trusted server configuration.",
                       provider_id=provider_id, provider_version=provider_version)

    if instrument != expected_instrument or symbol != expected_instrument:
        return _result("BLOCKED", "Provider evidence does not match the configured instrument and canonical symbol.",
                       provider_id=provider_id, provider_version=provider_version)

    if evidence.get("connectivity_status") != "CONNECTED":
        return _result("OFFLINE", "Configured provider is not reachable.",
                       provider_id=provider_id, provider_version=provider_version)

    if not evidence.get("latest_observation_at"):
        return _result("OFFLINE", "Provider returned no market observations.",
                       provider_id=provider_id, provider_version=provider_version)

    try:
        checked_at = _parse_aware_timestamp(evidence.get("checked_at"), "checked_at")
        latest = _parse_aware_timestamp(evidence.get("latest_observation_at"), "latest_observation_at")
        interval = _positive_integer(evidence.get("expected_interval_seconds"), "expected_interval_seconds")
        stale_after = _positive_integer(evidence.get("stale_after_seconds"), "stale_after_seconds")
        missing = _non_negative_integer(evidence.get("missing_candle_count"), "missing_candle_count")
        invalid = _non_negative_integer(evidence.get("invalid_candle_count"), "invalid_candle_count")
        future_count = _non_negative_integer(evidence.get("future_candle_count"), "future_candle_count")
        heartbeat_ttl = _positive_integer(evidence.get("heartbeat_stale_after_seconds"), "heartbeat_stale_after_seconds")
    except ValueError as exc:
        return _result("DEGRADED", "Provider evidence is malformed: " + str(exc),
                       provider_id=provider_id, provider_version=provider_version)

    if checked_at > checked_now or latest > checked_at or latest > checked_now:
        return _result("DEGRADED", "Provider evidence contains a future timestamp.",
                       checked_at=checked_at, latest_observation_at=latest,
                       provider_id=provider_id, provider_version=provider_version,
                       evidence_id=evidence.get("evidence_id"))

    heartbeat_age = (checked_now - checked_at).total_seconds()
    if heartbeat_age > heartbeat_ttl:
        return _result("OFFLINE", "Provider heartbeat has expired.",
                       checked_at=checked_at, latest_observation_at=latest,
                       provider_id=provider_id, provider_version=provider_version,
                       evidence_id=evidence.get("evidence_id"))

    age_seconds = (checked_at - latest).total_seconds()
    if age_seconds > stale_after or missing or invalid or future_count:
        return _result("DEGRADED", "Market observations are stale, incomplete, or invalid.",
                       checked_at=checked_at, latest_observation_at=latest,
                       age_seconds=age_seconds, provider_id=provider_id,
                       provider_version=provider_version, evidence_id=evidence.get("evidence_id"))

    # Verification flags come only from separate server-owned configuration;
    # the provider payload is never permitted to self-assert its own trust.
    if (not isinstance(trusted_configuration, dict)
            or trusted_configuration.get("provider_id") != trusted_provider_id):
        return _result("BLOCKED", "Trusted provider configuration is missing or mismatched.",
                       checked_at=checked_at, latest_observation_at=latest,
                       age_seconds=age_seconds, provider_id=provider_id,
                       provider_version=provider_version, evidence_id=evidence.get("evidence_id"))

    required_verified = (
        "source_verified",
        "symbol_mapping_verified",
        "provenance_verified",
        "broker_specification_verified",
    )
    if any(trusted_configuration.get(field) is not True for field in required_verified):
        return _result("BLOCKED", "Source, symbol mapping, provenance, or broker specifications are not verified.",
                       checked_at=checked_at, latest_observation_at=latest,
                       age_seconds=age_seconds, provider_id=provider_id,
                       provider_version=provider_version, evidence_id=evidence.get("evidence_id"))

    if evidence.get("coverage_complete") is not True:
        return _result("DEGRADED", "Configured monitoring-window coverage is incomplete.",
                       checked_at=checked_at, latest_observation_at=latest,
                       age_seconds=age_seconds, provider_id=provider_id,
                       provider_version=provider_version, evidence_id=evidence.get("evidence_id"))

    if not isinstance(provider_version, str) or not provider_version.strip():
        return _result("BLOCKED", "Provider version is missing.",
                       checked_at=checked_at, latest_observation_at=latest,
                       age_seconds=age_seconds, provider_id=provider_id,
                       evidence_id=evidence.get("evidence_id"))

    if not isinstance(evidence.get("evidence_id"), str) or not evidence["evidence_id"].strip():
        return _result("BLOCKED", "Stable provider evidence identifier is missing.",
                       checked_at=checked_at, latest_observation_at=latest,
                       age_seconds=age_seconds, provider_id=provider_id,
                       provider_version=provider_version)

    if not isinstance(evidence.get("source_reference"), str) or not evidence["source_reference"].strip():
        return _result("BLOCKED", "Provider source reference is missing.",
                       checked_at=checked_at, latest_observation_at=latest,
                       age_seconds=age_seconds, provider_id=provider_id,
                       provider_version=provider_version, evidence_id=evidence.get("evidence_id"))

    _ = interval  # Expected cadence is mandatory provider evidence.
    return _result("HEALTHY", "Configured XAUUSD market monitoring checks passed; execution remains separately gated.",
                   checked_at=checked_at, latest_observation_at=latest,
                   age_seconds=age_seconds, provider_id=provider_id,
                   provider_version=provider_version, evidence_id=evidence["evidence_id"])
