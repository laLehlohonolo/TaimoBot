# AWS IAM policy design — non-production draft

Status: **design only; not approved for attachment or deployment**.

Target architecture: ECS/Fargate application in `af-south-1`, Secrets Manager secret namespace
`taimobot/broker-bridge/*`, and one customer-managed KMS key. Replace every
`<ACCOUNT_ID>`, `<REGION>`, and `<KMS_KEY_ID>` placeholder only inside an
authorized AWS account after review. Do not commit real account identifiers,
role ARNs, credentials, secret ARNs, or key material.

This design separates:
- ECS task execution role (ECS agent: image pull and log delivery).
- Application provisioning role (create and recover secrets; tightly controlled).
- Application runtime retrieval role (read only).
- Human operator reconciliation role (separately approved and audited).

The application task role is not the task execution role. Do not put application
Secrets Manager permissions on the execution role. AWS distinguishes these roles:
https://docs.aws.amazon.com/AmazonECS/latest/developerguide/security-iam-roles.html

## 1. Provisioning task-role permissions — draft

The current adapter calls:
- `CreateSecret` with tags and a customer-managed KMS key.
- `DescribeSecret` and `GetSecretValue` to recover the exact operation version
  after an ambiguous create response.
- `DeleteSecret` only for fenced compensation after the provisioning store
  reserves cleanup state.

Because the adapter includes tags in `CreateSecret`, AWS requires
`secretsmanager:TagResource` as well as `secretsmanager:CreateSecret`.
Using a customer-managed KMS key for creation requires `kms:GenerateDataKey`
and `kms:Decrypt`. See:
https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_CreateSecret.html
https://docs.aws.amazon.com/secretsmanager/latest/userguide/security-encryption.html

Illustrative identity policy (not yet tested in an AWS account):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CreateOnlyBrokerBridgeSecrets",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:CreateSecret",
        "secretsmanager:TagResource"
      ],
      "Resource": "arn:aws:secretsmanager:<REGION>:<ACCOUNT_ID>:secret:taimobot/broker-bridge/*",
      "Condition": {
        "StringEquals": {
          "aws:RequestTag/TaimoBotPurpose": "broker-bridge-key"
        },
        "ForAllValues:StringEquals": {
          "aws:TagKeys": [
            "TaimoBotPurpose",
            "TaimoBotOperationId",
            "TaimoBotConnectionDigest"
          ]
        }
      }
    },
    {
      "Sid": "RecoverAndCompensateBrokerBridgeSecrets",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DeleteSecret"
      ],
      "Resource": "arn:aws:secretsmanager:<REGION>:<ACCOUNT_ID>:secret:taimobot/broker-bridge/*"
    },
    {
      "Sid": "UseOnlyTheBrokerBridgeKmsKeyThroughSecretsManager",
      "Effect": "Allow",
      "Action": [
        "kms:GenerateDataKey",
        "kms:Decrypt"
      ],
      "Resource": "arn:aws:kms:<REGION>:<ACCOUNT_ID>:key/<KMS_KEY_ID>",
      "Condition": {
        "StringEquals": {
          "kms:ViaService": "secretsmanager.<REGION>.amazonaws.com",
          "kms:CallerAccount": "<ACCOUNT_ID>"
        }
      }
    }
  ]
}
```

**Important review points**
- This role is high impact because it can delete secrets. Keep it isolated to a
  dedicated provisioning worker/service, not the general API runtime.
- The current adapter uses immediate deletion
  (`ForceDeleteWithoutRecovery=true`). Before granting `DeleteSecret`, review
  whether that destructive behavior is acceptable for non-production and define
  operator recovery controls. Prefer a separately isolated cleanup worker if
  the architecture can support it.
- The resource ARN wildcard must be tested against the actual Secrets Manager
  ARN suffix behavior and the service authorization model. Do not broaden to
  `Resource: "*"` as a workaround.
- The tag condition is defense in depth, not a substitute for resource scoping.
  Confirm that all required request-tag conditions work for both
  `CreateSecret` and `TagResource` in IAM policy simulation and live tests.
- The adapter's current tag set is exactly the three keys shown above.

## 2. Runtime retrieval task-role permissions — draft

The current adapter's read path calls only `GetSecretValue`. It does not call
`DescribeSecret`, list secrets, create, tag, rotate, or delete.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadOnlyBrokerBridgeSecretValues",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:<REGION>:<ACCOUNT_ID>:secret:taimobot/broker-bridge/*"
    },
    {
      "Sid": "DecryptBrokerBridgeSecretsThroughSecretsManager",
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "arn:aws:kms:<REGION>:<ACCOUNT_ID>:key/<KMS_KEY_ID>",
      "Condition": {
        "StringEquals": {
          "kms:ViaService": "secretsmanager.<REGION>.amazonaws.com",
          "kms:CallerAccount": "<ACCOUNT_ID>"
        }
      }
    }
  ]
}
```

Do not grant runtime `CreateSecret`, `TagResource`, `PutSecretValue`,
`UpdateSecret`, `RotateSecret`, `DeleteSecret`, `ListSecrets`, or
`BatchGetSecretValue`.

## 3. ECS task execution role

Keep this role separate from the application task role. Grant only the
permissions required by the chosen deployment features, typically scoped ECR
image-pull and CloudWatch log-delivery permissions. Do not attach either
Secrets Manager application policy above to the execution role.

If a future task definition injects a secret as an ECS container environment
secret, that is a different access path and must be reviewed separately; this
design assumes the application calls Secrets Manager using its task role.

## 4. KMS key policy requirements

The KMS key policy must enable the intended account/roles to use the key while
constraining cryptographic use to Secrets Manager in the selected region.
Identity policies alone are not sufficient if the key policy does not permit
the intended access.

Use the exact key ARN, and validate:
- provisioning role: GenerateDataKey and Decrypt through Secrets Manager;
- runtime role: Decrypt through Secrets Manager only;
- no participant bridge identity or general-purpose application role can use
  the key directly;
- key administration is separate from key use;
- CloudTrail records and alerts cover key-policy, grant, disable, schedule
  deletion, and access changes.

AWS documents `kms:ViaService` for constraining KMS use to Secrets Manager:
https://docs.aws.amazon.com/secretsmanager/latest/userguide/security-encryption.html

Do not copy a broad sample KMS key policy into an account without adapting and
reviewing its principals and conditions.

## 5. Required trust and deployment controls

Before creating roles:
1. Trust only `ecs-tasks.amazonaws.com` for ECS task roles.
2. Add and validate confused-deputy protections using `aws:SourceAccount` and
   the narrowest supported `aws:SourceArn` for the account/cluster.
3. Use separate task roles for provisioning and runtime services.
4. Use temporary ECS task-role credentials; no IAM user access keys, static
   access keys, or credentials in environment variables.
5. Restrict who can pass each role (`iam:PassRole`) to the deployment
   principal and only the intended ECS task/service.
6. Enable CloudTrail management events and alert on denied/ unusual secret and
   KMS operations.
7. Keep the region fixed to the reviewed region; do not silently fall back.
8. Apply permissions boundaries/SCPs where appropriate and verify effective
   permissions, not only the attached policy JSON.

## 6. Pre-attachment validation checklist

- [ ] Confirm account ownership and explicit authorization for a non-production
      AWS environment.
- [ ] Confirm `af-south-1` is enabled and required services/quotas are available.
- [ ] Replace placeholders locally; never commit actual account or key identifiers.
- [ ] Validate JSON syntax and IAM action/resource support with AWS IAM Access
      Analyzer and policy validation.
- [ ] Simulate allowed and denied calls for both roles.
- [ ] Prove runtime role cannot create, tag, describe (unless separately needed),
      rotate, update, or delete secrets.
- [ ] Prove provisioning role cannot access unrelated secrets or unrelated KMS
      keys.
- [ ] Test tag conditions, secret ARN matching, KMS ViaService conditions and
      key policy interaction in the non-production account.
- [ ] Test ambiguous create recovery, concurrent retries, exact version lookup,
      rotation, revocation and cleanup under failure injection.
- [ ] Review immediate deletion and operator reconciliation before granting
      destructive permission.
- [ ] Record CloudTrail evidence and verify no secret bytes appear in logs,
      traces, API responses or database rows.
- [ ] Obtain a second-person security review before attaching policies.

## Current implementation boundary

These are **policy design examples**, not provisioned AWS policies. No IAM role,
KMS key, AWS secret, AWS account integration, or deployment has been created.
The AWS adapter is injected-client code with fake-client tests only. Live AWS
service semantics, IAM evaluation, KMS key policy behavior and operational
recovery remain unverified. No broker connection or live order capability is
enabled.
