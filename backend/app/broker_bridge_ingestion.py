"""Fail-closed composition service for read-only broker evidence ingestion.

This is an internal application service, not an HTTP route. Deployment must
supply a least-privilege SecretReader. No participant identity is accepted from
the payload, and no broker order or account-control capability exists here.
"""
import json
from datetime import datetime, timezone

from .broker_bridge_evidence import (
    persist_authenticated_bridge_evidence,
    validate_bridge_evidence,
)
from .broker_bridge_security import authenticate_bridge_request
from .broker_bridge_vault import load_bridge_key_secret


MAX_EVIDENCE_BODY_BYTES = 16 * 1024
MAX_PROVIDER_AGE_SECONDS = 15 * 60
MAX_PROVIDER_FUTURE_SKEW_SECONDS = 60


class BridgeIngestionRequestError(ValueError):
    """Generic request rejection safe to return without leaking secret data."""


def _reject_constant(_value):
    raise BridgeIngestionRequestError("Evidence JSON contains a non-finite number.")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BridgeIngestionRequestError("Evidence JSON contains a duplicate field.")
        result[key] = value
    return result


def _parse_body(raw_body, content_type):
    if not isinstance(content_type, str):
        raise BridgeIngestionRequestError("A JSON content type is required.")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise BridgeIngestionRequestError("Only application/json evidence is accepted.")
    if not isinstance(raw_body, bytes) or not raw_body:
        raise BridgeIngestionRequestError("Evidence body must be non-empty bytes.")
    if len(raw_body) > MAX_EVIDENCE_BODY_BYTES:
        raise BridgeIngestionRequestError("Evidence body exceeds the allowed size.")
    try:
        decoded = raw_body.decode("utf-8", errors="strict")
        evidence = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except BridgeIngestionRequestError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise BridgeIngestionRequestError("Evidence body is not valid UTF-8 JSON.") from None
    if not isinstance(evidence, dict):
        raise BridgeIngestionRequestError("Evidence JSON must be an object.")
    return evidence


def _validate_provider_freshness(evidence, now_epoch):
    value = evidence.get("provider_timestamp")
    if not isinstance(value, str):
        raise BridgeIngestionRequestError("Provider timestamp is required.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise BridgeIngestionRequestError("Provider timestamp is invalid.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BridgeIngestionRequestError("Provider timestamp must include a timezone.")
    age = now_epoch - parsed.timestamp()
    if age > MAX_PROVIDER_AGE_SECONDS:
        raise BridgeIngestionRequestError("Provider evidence is stale.")
    if age < -MAX_PROVIDER_FUTURE_SKEW_SECONDS:
        raise BridgeIngestionRequestError("Provider timestamp is too far in the future.")


def ingest_authenticated_bridge_evidence(
    *,
    connection_id,
    key_id,
    timestamp,
    nonce,
    signature,
    raw_body,
    content_type,
    secret_reader,
    now=None,
):
    """Authenticate and persist one participant-scoped HEARTBEAT or QUOTE.

    The connection ID is an untrusted route selector until it matches the
    server-resolved key context. Participant ownership is always read from the
    database by the evidence persistence layer. HMAC authenticates possession
    of the bridge key; it does not prove broker provenance.
    """
    evidence = _parse_body(raw_body, content_type)
    now_epoch = int(datetime.now(timezone.utc).timestamp()) if now is None else int(now)
    _validate_provider_freshness(evidence, now_epoch)
    evidence = validate_bridge_evidence(evidence)

    try:
        context = load_bridge_key_secret(key_id, secret_reader)
    except Exception:
        raise BridgeIngestionRequestError("Bridge authentication is unavailable.") from None
    if context.get("connection_id") != connection_id or context.get("key_id") != key_id:
        raise BridgeIngestionRequestError("Bridge connection identity is invalid.")

    authentication = authenticate_bridge_request(
        connection_id,
        timestamp,
        nonce,
        signature,
        raw_body,
        context["shared_secret"],
        key_id=key_id,
        now=now_epoch,
    )
    result = persist_authenticated_bridge_evidence(
        authentication["connection_id"],
        evidence,
    )
    return {
        "status": result["status"],
        "connection_id": result["connection_id"],
        "evidence_id": result["evidence_id"],
        "evidence_kind": result["evidence_kind"],
        "received_at": result.get("received_at"),
        "read_only": True,
        "live_order_routing": False,
    }
