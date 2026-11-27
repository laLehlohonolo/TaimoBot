"""Vault-neutral broker bridge key provisioning orchestration.\n\nThis module defines a narrow production secret-manager contract but ships no\ncloud-vault adapter or HTTP endpoint. Secret bytes are generated server-side\nand never returned.\n"""
import secrets
import uuid
from typing import Protocol

from .broker_bridge_keys import (
    BridgeKeyActivationLeaseError,
    activate_bridge_key,
)
from .database import get_connection
from . import broker_bridge_provisioning_store as operation_store


class SecretManager(Protocol):
    """Minimal secret-manager contract; implementations must encrypt at rest."""

    def create_secret(self, *, name: str, value: bytes, idempotency_key: str) -> str:
        """Persist secret bytes idempotently and return an opaque vault reference."""
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
    """Create vault material, then activate metadata under a fenced lease.\n\n    A production adapter must provide encrypted, private, auditable, idempotent\n    secret storage. Operation completion is committed atomically with key\n    metadata, and stale workers cannot perform compensation or overwrite state.\n    """
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

    with get_connection() as connection:
        actor = connection.execute(
            "SELECT role, email_verified FROM users WHERE user_id = ?",
            (created_by,),
        ).fetchone()
        broker_connection = connection.execute(
            "SELECT state FROM participant_broker_connections WHERE connection_id = ?",
            (connection_id,),
        ).fetchone()
    if actor is None or actor["role"] != "ADMIN" or int(actor["email_verified"]) != 1:
        raise BridgeKeyProvisioningError("A verified administrator is required.")
    if broker_connection is None or broker_connection["state"] != "REQUESTED":
        raise BridgeKeyProvisioningError("An active broker connection is required.")

    operation_store.ensure_schema()
    try:
        operation = operation_store.load_or_create(operation_id, connection_id, created_by)
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
            raise BridgeKeyProvisioningError(
                "Vault creation outcome is uncertain; retry with the same operation ID."
            ) from None
        finally:
            del secret_material
        if not isinstance(secret_ref, str) or not secret_ref:
            raise BridgeKeyProvisioningError(
                "Vault returned no usable reference; retry or reconcile the same operation."
            )
        operation_store.update(operation_id, "SECRET_CREATED", secret_ref=secret_ref)

    if not isinstance(secret_ref, str) or not secret_ref:
        raise BridgeKeyProvisioningError("Provisioning operation requires reconciliation.")

    activation_token = operation_store.claim_activation(operation_id)
    if activation_token is None:
        raise BridgeKeyProvisioningError(
            "Provisioning activation is already in progress; retry with the same operation ID."
        )

    # Reconcile only under the newly claimed token. This covers legacy records
    # created before key metadata and operation completion became atomic.
    with get_connection() as connection:
        existing_key = connection.execute(
            "SELECT key_id FROM broker_bridge_keys WHERE secret_ref = ? AND connection_id = ?",
            (secret_ref, connection_id),
        ).fetchone()
    if existing_key is not None:
        if operation_store.complete_existing_activation(
            operation_id, connection_id, activation_token, existing_key["key_id"]
        ):
            recovered = operation_store.key_metadata(operation_id, connection_id)
            if recovered is not None:
                return recovered
        raise BridgeKeyProvisioningError(
            "Existing key metadata could not be reconciled under the current activation lease."
        )

    try:
        return activate_bridge_key(
            connection_id, secret_ref, created_by=created_by, now=now,
            operation_id=operation_id, activation_token=activation_token,
        )
    except BridgeKeyActivationLeaseError:
        # A newer worker may own or have completed this operation. Never delete
        # a secret that the new owner may already have activated.
        raise BridgeKeyProvisioningError(
            "Activation lease ownership changed; no vault cleanup was attempted."
        ) from None
    except Exception:
        # First fence the operation into a non-reclaimable cleanup state. Only
        # after that reservation succeeds may this worker delete the secret.
        reserved = operation_store.update_activation_owner(
            operation_id, activation_token, "CLEANUP_REQUIRED",
            error_code="ACTIVATION_FAILED_CLEANUP_PENDING",
        )
        if not reserved:
            raise BridgeKeyProvisioningError(
                "Activation failed after lease ownership changed; operator reconciliation is required."
            ) from None
        try:
            secret_manager.delete_secret(secret_ref)
        except Exception:
            raise BridgeKeyProvisioningError(
                "Key activation failed and vault cleanup also failed; operator cleanup is required."
            ) from None
        operation_store.update(
            operation_id, "FAILED", error_code="ACTIVATION_FAILED"
        )
        raise BridgeKeyProvisioningError(
            "Bridge key provisioning failed; no key was activated."
        ) from None
