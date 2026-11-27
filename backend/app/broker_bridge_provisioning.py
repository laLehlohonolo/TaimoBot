"""Vault-neutral broker bridge key provisioning orchestration.

This module defines the narrow contract expected from a production secret
manager. It deliberately ships no cloud-vault adapter and exposes no HTTP
endpoint. Secret bytes are generated server-side and never returned.
"""
import secrets
import uuid
from typing import Protocol

from .broker_bridge_keys import activate_bridge_key
from .database import get_connection


class SecretManager(Protocol):
    """Minimal secret-manager contract; implementations must encrypt at rest."""

    def create_secret(
        self, *, name: str, value: bytes, idempotency_key: str
    ) -> str:
        """Persist secret bytes idempotently and return an opaque vault reference.

        Repeating a call with the same idempotency key must return the same
        secret reference and must never create a second secret.
        """
        ...

    def delete_secret(self, secret_ref: str) -> None:
        """Permanently delete an uncommitted secret reference."""
        ...


class BridgeKeyProvisioningError(RuntimeError):
    """Raised without including secret bytes or secret-manager response details."""


def provision_bridge_key(
    connection_id: str,
    created_by: str,
    secret_manager: SecretManager,
    *,
    now: str | None = None,
    operation_id: str | None = None,
) -> dict:
    """Create vault material, then activate metadata; compensate on DB failure.

    A production adapter must enforce private access, encryption at rest,
    auditability and least privilege. This function never logs or returns the
    generated 256-bit key. If metadata activation fails, it attempts to delete
    the just-created vault item. A failed compensation is surfaced explicitly
    for operator cleanup; the reference is never returned to the caller.
    """
    if not isinstance(connection_id, str) or not connection_id:
        raise BridgeKeyProvisioningError("A valid broker connection is required.")
    if not isinstance(created_by, str) or not created_by:
        raise BridgeKeyProvisioningError("A verified administrator is required.")
    # Callers that need crash/retry recovery must persist this operation ID
    # before invoking the vault and pass the same ID on every retry.
    operation_id = operation_id or str(uuid.uuid4())
    try:
        operation_id = str(uuid.UUID(operation_id))
    except (AttributeError, TypeError, ValueError):
        raise BridgeKeyProvisioningError("A valid provisioning operation ID is required.") from None
    if not callable(getattr(secret_manager, "create_secret", None)) or not callable(
        getattr(secret_manager, "delete_secret", None)
    ):
        raise BridgeKeyProvisioningError("A compatible secret manager is required.")

    # Preflight authorization and connection state before creating vault material.
    # activate_bridge_key repeats these checks transactionally to close TOCTOU gaps.
    with get_connection() as connection:
        actor = connection.execute(
            "SELECT role, email_verified FROM users WHERE user_id = ?",
            (created_by,),
        ).fetchone()
        broker_connection = connection.execute(
            "SELECT state FROM participant_broker_connections WHERE connection_id = ?",
            (connection_id,),
        ).fetchone()
    if (
        actor is None
        or actor["role"] != "ADMIN"
        or int(actor["email_verified"]) != 1
    ):
        raise BridgeKeyProvisioningError("A verified administrator is required.")
    if broker_connection is None or broker_connection["state"] != "REQUESTED":
        raise BridgeKeyProvisioningError("An active broker connection is required.")

    secret_material = secrets.token_bytes(32)
    secret_ref = None
    try:
        secret_ref = secret_manager.create_secret(
            name=f"taimobot/broker-bridge/{connection_id}/{operation_id}",
            value=secret_material,
            idempotency_key=operation_id,
        )
        return activate_bridge_key(
            connection_id,
            secret_ref,
            created_by=created_by,
            now=now,
        )
    except Exception:
        if isinstance(secret_ref, str) and secret_ref:
            try:
                secret_manager.delete_secret(secret_ref)
            except Exception:
                raise BridgeKeyProvisioningError(
                    "Key activation failed and vault cleanup also failed; operator cleanup is required."
                ) from None
        raise BridgeKeyProvisioningError(
            "Bridge key provisioning failed; no key was activated."
        ) from None
    finally:
        # Immutable bytes cannot be reliably zeroized in Python; keep scope short.
        del secret_material
