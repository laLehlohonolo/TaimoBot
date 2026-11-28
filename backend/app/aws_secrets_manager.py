"""AWS Secrets Manager adapter for broker-bridge keys.

The adapter is deliberately dependency-injected: production supplies a boto3
Secrets Manager client authenticated with the ECS task role. No static AWS
credentials are accepted here.
"""
import hashlib
import re
import secrets
import uuid
from typing import Any


_REF_PREFIX = "secretmanager://"
_NAME_PREFIX = "taimobot/broker-bridge/"
_ARN_RE = re.compile(
    r"^arn:[a-z0-9-]+:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+$"
)
_MIN_SECRET_BYTES = 32


class AWSSecretManagerError(RuntimeError):
    """Generic adapter error that never includes AWS response details."""


def _error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return None
    metadata = response.get("Error")
    if not isinstance(metadata, dict):
        return None
    code = metadata.get("Code")
    return code if isinstance(code, str) else None


def _opaque_ref(arn: str) -> str:
    if not isinstance(arn, str) or not _ARN_RE.fullmatch(arn):
        raise AWSSecretManagerError("Secret-manager operation failed.")
    return _REF_PREFIX + arn


def _secret_arn(secret_ref: str) -> str:
    if not isinstance(secret_ref, str) or not secret_ref.startswith(_REF_PREFIX):
        raise AWSSecretManagerError("Secret-manager operation failed.")
    arn = secret_ref[len(_REF_PREFIX):]
    if not _ARN_RE.fullmatch(arn):
        raise AWSSecretManagerError("Secret-manager operation failed.")
    return arn


class AWSSecretsManagerAdapter:
    """Implements provisioning and retrieval using one scoped AWS client.

    The supplied client must use the workload's ECS task role. The caller must
    scope that role to the specific secret namespace and KMS key. A customer-
    managed KMS key is mandatory; static access keys are not accepted.
    """

    def __init__(self, client: Any, *, kms_key_id: str):
        if client is None or not isinstance(kms_key_id, str) or not kms_key_id.strip():
            raise ValueError("An AWS Secrets Manager client and KMS key are required.")
        self._client = client
        self._kms_key_id = kms_key_id.strip()

    @staticmethod
    def _validate_operation(name: str, idempotency_key: str) -> str:
        try:
            operation_id = str(uuid.UUID(idempotency_key))
        except (AttributeError, TypeError, ValueError):
            raise AWSSecretManagerError("Secret-manager operation failed.") from None
        if (
            not isinstance(name, str)
            or not name.startswith(_NAME_PREFIX)
            or not name.endswith("/" + operation_id)
            or len(name) > 512
            or any(part in ("", ".", "..") for part in name.split("/"))
        ):
            raise AWSSecretManagerError("Secret-manager operation failed.")
        return operation_id

    @staticmethod
    def _tags(name: str, operation_id: str) -> list[dict[str, str]]:
        connection_segment = name[len(_NAME_PREFIX):].rsplit("/", 1)[0]
        connection_digest = hashlib.sha256(connection_segment.encode("utf-8")).hexdigest()
        return [
            {"Key": "TaimoBotPurpose", "Value": "broker-bridge-key"},
            {"Key": "TaimoBotOperationId", "Value": operation_id},
            {"Key": "TaimoBotConnectionDigest", "Value": connection_digest},
        ]

    def _recover_created_secret(self, name: str, operation_id: str) -> str:
        """Recover the exact version after an ambiguous create response."""
        try:
            metadata = self._client.describe_secret(SecretId=name)
            expected = {(tag["Key"], tag["Value"]) for tag in self._tags(name, operation_id)}
            actual = {
                (tag.get("Key"), tag.get("Value"))
                for tag in metadata.get("Tags", [])
                if isinstance(tag, dict)
            }
            if not expected.issubset(actual):
                raise AWSSecretManagerError("Secret-manager operation failed.")
            arn = metadata.get("ARN")
            version = self._client.get_secret_value(
                SecretId=arn or name,
                VersionId=operation_id,
            )
            if not isinstance(version.get("SecretBinary"), bytes) or len(version["SecretBinary"]) < _MIN_SECRET_BYTES:
                raise AWSSecretManagerError("Secret-manager operation failed.")
            return _opaque_ref(arn)
        except AWSSecretManagerError:
            raise
        except Exception:
            raise AWSSecretManagerError("Secret-manager operation failed.") from None

    def create_secret(self, *, name: str, idempotency_key: str) -> str:
        """Create one 256-bit key; retries recover the original AWS version."""
        operation_id = self._validate_operation(name, idempotency_key)
        material = secrets.token_bytes(_MIN_SECRET_BYTES)
        try:
            result = self._client.create_secret(
                Name=name,
                Description="TaimoBot participant-scoped broker bridge authentication key",
                SecretBinary=material,
                ClientRequestToken=operation_id,
                KmsKeyId=self._kms_key_id,
                Tags=self._tags(name, operation_id),
            )
            return _opaque_ref(result.get("ARN"))
        except Exception:
            # AWS binds ClientRequestToken to a version. A retry with different
            # generated bytes cannot overwrite it; recover the exact committed
            # version instead of returning or persisting newly generated bytes.
            return self._recover_created_secret(name, operation_id)

    def get_secret(self, secret_ref: str) -> bytes:
        """Read only the current binary secret version; never cache it."""
        arn = _secret_arn(secret_ref)
        try:
            result = self._client.get_secret_value(
                SecretId=arn,
                VersionStage="AWSCURRENT",
            )
            value = result.get("SecretBinary")
            if not isinstance(value, bytes) or len(value) < _MIN_SECRET_BYTES:
                raise AWSSecretManagerError("Secret-manager operation failed.")
            return value
        except AWSSecretManagerError:
            raise
        except Exception:
            raise AWSSecretManagerError("Secret-manager operation failed.") from None

    def delete_secret(self, secret_ref: str) -> None:
        """Immediately delete an uncommitted secret during fenced compensation.

        This destructive operation must only be called by the provisioning
        orchestrator after it has reserved the current operation's cleanup
        state. The runtime retrieval role must not receive DeleteSecret.
        """
        arn = _secret_arn(secret_ref)
        try:
            self._client.delete_secret(
                SecretId=arn,
                ForceDeleteWithoutRecovery=True,
            )
        except Exception:
            raise AWSSecretManagerError("Secret-manager operation failed.") from None
