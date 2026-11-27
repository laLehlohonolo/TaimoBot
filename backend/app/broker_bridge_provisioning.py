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
from . import broker_bridge_provisioning_store as operation_store


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
    operation_id: str,
    now: str | None = None,
) -> dict:
    """Create vault material, then activate metadata; compensate on DB failure.

    A production adapter must enforce private access, encryption at rest,
    auditability, least privilege, and idempotent create semantics. This
    function persists operation state before vault access, never logs or
    returns the generated 256-bit key, and records cleanup failures for review.
    """
    if not isinstance(connection_id, str) or not connection_id:
        raise BridgeKeyProvisioningError("A valid broker connection is required.")
    if not isinstance(created_by, str) or not created_by:
        raise BridgeKeyProvisioningError("A verified administrator is required.")
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

    operation_store.ensure_schema()
    try:
        operation = operation_store.load_or_create(
            operation_id, connection_id, created_by
        )
    except ValueError:
        raise BridgeKeyProvisioningError(
            "Provisioning operation does not match its original connection and actor."
        ) from None

    if operation["status"] == "ACTIVATED":
        existing = operation_store.key_metadata(operation_id, connection_id)
        if existing is not None:
            return existing
        raise BridgeKeyProvisioningError("Provisioning operation is already finalized.")
    if operation["status"] == "CLEANUP_REQUIRED":
        raise BridgeKeyProvisioningError(
            "Provisioning requires operator vault cleanup before retry."
        )
    if operation["status"] == "FAILED":
        raise BridgeKeyProvisioningError("Provisioning operation has permanently failed.")

    secret_ref = operation.get("secret_ref")
    if operation["status"] == "PENDING":
        secret_material = secrets.token_bytes(32)
        try:
            secret_ref = secret_manager.create_secret(
                name=f"taimobot/broker-bridge/{connection_id}/{operation_id}",
                value=secret_material,
                idempotency_key=operation_id,
            )
        except Exception:
            # A timeout may mean the vault committed but its response was lost.
            # Keep PENDING and retry with the same durable idempotency key.
            raise BridgeKeyProvisioningError(
                "Vault creation outcome is uncertain; retry with the same operation ID."
            ) from None
        finally:
            # Immutable bytes cannot be reliably zeroized in Python.
            del secret_material
        if not isinstance(secret_ref, str) or not secret_ref:
            raise BridgeKeyProvisioningError(
                "Vault returned no usable reference; retry or reconcile the same operation."
            )
        operation_store.update(
            operation_id, "SECRET_CREATED", secret_ref=secret_ref
        )

    if not isinstance(secret_ref, str) or not secret_ref:
        raise BridgeKeyProvisioningError("Provisioning operation requires reconciliation.")

    # Recover if metadata activation committed but the operation status update
    # was interrupted by a process crash.
    with get_connection() as connection:
        existing_key = connection.execute(
            "SELECT key_id FROM broker_bridge_keys WHERE secret_ref = ? AND connection_id = ?",
            (secret_ref, connection_id),
        ).fetchone()
    if existing_key is not None:
        operation_store.update(
            operation_id, "ACTIVATED", key_id=existing_key["key_id"]
        )
        recovered = operation_store.key_metadata(operation_id, connection_id)
        if recovered is not None:
            return recovered

    try:
        result = activate_bridge_key(
            connection_id, secret_ref, created_by=created_by, now=now
        )
    except Exception:
        try:
            secret_manager.delete_secret(secret_ref)
        except Exception:
            operation_store.update(
                operation_id, "CLEANUP_REQUIRED",
                error_code="VAULT_CLEANUP_FAILED",
            )
            raise BridgeKeyProvisioningError(
                "Key activation failed and vault cleanup also failed; operator cleanup is required."
            ) from None
        operation_store.update(
            operation_id, "FAILED", error_code="ACTIVATION_FAILED"
        )
        raise BridgeKeyProvisioningError(
            "Bridge key provisioning failed; no key was activated."
        ) from None

    # If this status write fails after activation, leave the vault secret intact.
    # A retry finds the committed key by secret_ref and repairs the operation row.
    operation_store.update(
        operation_id, "ACTIVATED", key_id=result["key_id"]
    )
    return result
