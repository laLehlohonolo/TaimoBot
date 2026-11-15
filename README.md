# TaimoBot
it's a trading bot called TaimoBot, designed to help users with Forex trading by providing accurate predictions.


## Global emergency-stop administration

The global emergency-stop activation and resume endpoints require an authenticated
user whose email is explicitly listed in the backend environment variable
`TAIMOBOT_ADMIN_EMAILS`. Configure one or more administrator email addresses
as a comma-separated list, for example:

```text
TAIMOBOT_ADMIN_EMAILS=operator@example.com,owner@example.com
```

The allowlist is empty by default, so ordinary registered users cannot activate
or clear the system-wide stop. Keep this setting in the backend deployment's
secret/environment configuration, not in frontend code. This is an environment-
configured administrator allowlist, not a full role-management system.
