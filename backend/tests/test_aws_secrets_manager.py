import unittest
import uuid

from app.aws_secrets_manager import (
    AWSSecretManagerError,
    AWSSecretsManagerAdapter,
)


class FakeAWSClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeSecretsManagerClient:
    def __init__(self):
        self.secrets = {}
        self.fail_after_create = False
        self.fail_create = False
        self.last_create = None
        self.delete_calls = []

    def create_secret(self, **kwargs):
        self.last_create = kwargs
        if self.fail_create:
            raise FakeAWSClientError("AccessDeniedException")
        name = kwargs["Name"]
        token = kwargs["ClientRequestToken"]
        if name in self.secrets:
            existing = self.secrets[name]
            if token in existing["versions"]:
                if existing["versions"][token] != kwargs["SecretBinary"]:
                    raise FakeAWSClientError("InvalidRequestException")
                return {"ARN": existing["arn"]}
            raise FakeAWSClientError("ResourceExistsException")
        arn = "arn:aws:secretsmanager:af-south-1:123456789012:secret:" + name + "-abc123"
        self.secrets[name] = {
            "arn": arn,
            "tags": kwargs["Tags"],
            "versions": {token: kwargs["SecretBinary"]},
            "current": token,
        }
        if self.fail_after_create:
            self.fail_after_create = False
            raise TimeoutError("simulated lost response")
        return {"ARN": arn}

    def describe_secret(self, SecretId):
        secret = self._find(SecretId)
        return {"ARN": secret["arn"], "Tags": secret["tags"]}

    def get_secret_value(self, SecretId, VersionId=None, VersionStage=None):
        secret = self._find(SecretId)
        version = VersionId or secret["current"]
        if version not in secret["versions"]:
            raise FakeAWSClientError("ResourceNotFoundException")
        return {"ARN": secret["arn"], "SecretBinary": secret["versions"][version]}

    def delete_secret(self, **kwargs):
        self.delete_calls.append(kwargs)
        secret = self._find(kwargs["SecretId"])
        del self.secrets[secret["name"]]
        return {}

    def _find(self, identifier):
        for name, secret in self.secrets.items():
            if identifier in (name, secret["arn"]):
                result = dict(secret)
                result["name"] = name
                return result
        raise FakeAWSClientError("ResourceNotFoundException")


class AWSSecretsManagerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeSecretsManagerClient()
        self.adapter = AWSSecretsManagerAdapter(
            self.client,
            kms_key_id="arn:aws:kms:af-south-1:123456789012:key/test-key",
        )
        self.operation_id = str(uuid.uuid4())
        self.name = "taimobot/broker-bridge/connection-digest/" + self.operation_id

    def test_create_uses_256_bit_binary_secret_customer_kms_and_operation_token(self):
        ref = self.adapter.create_secret(name=self.name, idempotency_key=self.operation_id)
        self.assertTrue(ref.startswith("secretmanager://arn:aws:secretsmanager:"))
        self.assertEqual(len(self.client.last_create["SecretBinary"]), 32)
        self.assertEqual(self.client.last_create["KmsKeyId"], self.adapter._kms_key_id)
        self.assertEqual(self.client.last_create["ClientRequestToken"], self.operation_id)
        self.assertIn(("TaimoBotOperationId", self.operation_id), {
            (tag["Key"], tag["Value"]) for tag in self.client.last_create["Tags"]
        })

    def test_ambiguous_create_recovers_exact_committed_version(self):
        self.client.fail_after_create = True
        ref = self.adapter.create_secret(name=self.name, idempotency_key=self.operation_id)
        secret = next(iter(self.client.secrets.values()))
        original = secret["versions"][self.operation_id]
        self.assertEqual(self.adapter.get_secret(ref), original)
        self.assertEqual(len(secret["versions"]), 1)

    def test_retry_after_lost_response_recovers_same_bytes(self):
        self.client.fail_after_create = True
        first_ref = self.adapter.create_secret(name=self.name, idempotency_key=self.operation_id)
        first_bytes = self.adapter.get_secret(first_ref)
        second_ref = self.adapter.create_secret(name=self.name, idempotency_key=self.operation_id)
        self.assertEqual(first_ref, second_ref)
        self.assertEqual(self.adapter.get_secret(second_ref), first_bytes)
        self.assertEqual(len(next(iter(self.client.secrets.values()))["versions"]), 1)

    def test_wrong_operation_binding_is_rejected(self):
        with self.assertRaises(AWSSecretManagerError):
            self.adapter.create_secret(name=self.name, idempotency_key=str(uuid.uuid4()))

    def test_invalid_name_is_rejected(self):
        with self.assertRaises(AWSSecretManagerError):
            self.adapter.create_secret(name="unrelated/name", idempotency_key=self.operation_id)

    def test_untrusted_or_malformed_reference_is_rejected(self):
        for ref in ("vault://somewhere/key", "secretmanager://https://evil", "secretmanager://arn:bad"):
            with self.subTest(ref=ref), self.assertRaises(AWSSecretManagerError):
                self.adapter.get_secret(ref)

    def test_secret_string_and_short_binary_fail_closed(self):
        ref = self.adapter.create_secret(name=self.name, idempotency_key=self.operation_id)
        secret = next(iter(self.client.secrets.values()))
        secret["versions"][self.operation_id] = b"short"
        with self.assertRaises(AWSSecretManagerError):
            self.adapter.get_secret(ref)

    def test_tag_mismatch_fails_closed_during_ambiguous_recovery(self):
        self.client.fail_after_create = True
        original = self.client.describe_secret

        def wrong_tags(SecretId):
            metadata = original(SecretId)
            metadata["Tags"] = []
            return metadata

        self.client.describe_secret = wrong_tags
        with self.assertRaises(AWSSecretManagerError):
            self.adapter.create_secret(name=self.name, idempotency_key=self.operation_id)

    def test_delete_is_forceful_and_uses_only_parsed_secret_arn(self):
        ref = self.adapter.create_secret(name=self.name, idempotency_key=self.operation_id)
        self.adapter.delete_secret(ref)
        self.assertTrue(self.client.delete_calls[0]["ForceDeleteWithoutRecovery"])
        self.assertTrue(self.client.delete_calls[0]["SecretId"].startswith("arn:aws:secretsmanager:"))
        self.assertEqual(self.client.secrets, {})

    def test_create_outage_fails_closed_without_leaking_aws_error(self):
        self.client.fail_create = True
        with self.assertRaises(AWSSecretManagerError) as raised:
            self.adapter.create_secret(name=self.name, idempotency_key=self.operation_id)
        self.assertNotIn("AccessDeniedException", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
