"""Fail-closed retrieval boundary for broker bridge secrets.

A concrete cloud-vault adapter is intentionally injected by deployment.
This module never caches secrets, accepts caller-supplied vault references,
or returns secret material in metadata.
"""
from typing import Protocol

from .broker_bridge_keys import resolve_bridge_key_auth_context


class SecretReader(Protocol):
    """Least-privilege adapter for reading one opaque secret reference."""

    def get_secret(self, secret_ref: str) -> bytes:
        """Return the stored secret bytes or raise an adapter exception."""
        ...


class BridgeSecretUnavailable(RuntimeError):
    """Generic error that does not disclose key, tenant, or vault details."""


def _valid_secret_ref(value):
    return (
        isinstance(value, str)
        and 8 <= len(value) <= 240
        and value.startswith(("vault://", "secretmanager://"))
        and not any(marker in value.casefold() for marker in ("password=", "token=", "secret="))
    )


def load_bridge_key_secret(key_id: str, secret_reader: SecretReader) -> dict:
    """Resolve an active key, read its secret, then recheck binding before use.

    No cache is used. The second metadata lookup narrows the rotation/revocation
    race between the first lookup and vault retrieval. Authentication must still
    recheck connection state transactionally when consuming the request nonce.
    """
    if not isinstance(key_id, str) or not key_id:
        raise BridgeSecretUnavailable("Bridge authentication is unavailable.")
    if not callable(getattr(secret_reader, "get_secret", None)):
        raise BridgeSecretUnavailable("Bridge authentication is unavailable.")

    context = resolve_bridge_key_auth_context(key_id)
    if context is None or not _valid_secret_ref(context.get("secret_ref")):
        raise BridgeSecretUnavailable("Bridge authentication is unavailable.")

    try:
        secret = secret_reader.get_secret(context["secret_ref"])
    except Exception:
        raise BridgeSecretUnavailable("Bridge authentication is unavailable.") from None

    if not isinstance(secret, bytes) or len(secret) < 32:
        raise BridgeSecretUnavailable("Bridge authentication is unavailable.")

    current = resolve_bridge_key_auth_context(key_id)
    if current != context:
        raise BridgeSecretUnavailable("Bridge authentication is unavailable.")

    return {
        "key_id": context["key_id"],
        "connection_id": context["connection_id"],
        "participant_user_id": context["participant_user_id"],
        "shared_secret": secret,
        "secret_cached": False,
    }
