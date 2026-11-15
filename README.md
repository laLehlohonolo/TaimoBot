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


## Browser origin security

Cross-origin browser requests are disabled unless the backend is configured with
`TAIMOBOT_CORS_ORIGINS`, a comma-separated list of exact trusted origins, for
example:

```text
TAIMOBOT_CORS_ORIGINS=https://dashboard.example.com
```

Only explicit HTTP(S) origins are accepted; wildcard origins, URL paths, query
strings and fragments are rejected at startup. Bearer-token access does not use
cookies, so cross-origin credentials are disabled. Leave the variable unset for
same-origin deployments or local Vite proxy development.


## Emergency-stop persistence

The global emergency-stop flag is stored in the backend SQLite database, not only
in process memory. It therefore remains active across API process restarts and is
shared by backend workers using the same database. If the singleton safety-state
record is unexpectedly missing, the API treats the state as stopped until an
authorized administrator repairs or resumes it.
