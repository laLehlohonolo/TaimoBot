# TaimoBot frontend

React + Vite dashboard for the TaimoBot FastAPI paper-trading backend.

## Local development

1. Start the backend from the repository's `backend` directory:
   `uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`
2. In a second terminal, enter `frontend`, install dependencies with `npm install`, then run `npm run dev`.
3. Open the Vite address shown in the terminal. The development server proxies `/api` requests to the local FastAPI service.

## Configuration

Set `VITE_API_BASE_URL` to the API base path or URL. The default is `/api`.

## Authentication and security

- Register and sign in through the backend authentication endpoints.
- Private account, trade, position-group and copy-relationship endpoints require a bearer session and verify account ownership server-side.
- Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes. Session tokens are random, expire after 12 hours, and are stored server-side only as SHA-256 hashes.
- Repeated failed logins are temporarily throttled.
- The frontend keeps the bearer token in React memory only. Refreshing the page clears the session and requires signing in again. The token is not written to localStorage or sessionStorage.
- The dashboard is read-only for trade execution. It can create paper accounts and inspect account balances, risk and trade history, but it cannot place or close orders.
- Health and emergency-stop status are public read-only endpoints. Emergency-stop activation and resume require a persisted `ADMIN` role and verified email state in the backend database. Public registration creates participant accounts only.

**Deployment warning:** the database-backed role check is implemented, but secure first-administrator provisioning and email-verification delivery are not yet implemented. Administrator controls therefore fail closed until those components are completed. Do not expose the backend publicly until HTTPS, production CORS/origin restrictions, secret management, backups, monitoring and deployment hardening have been configured. Never place API keys, signing secrets, passwords or broker credentials in `VITE_*` variables; these are visible in browser assets.

All trading and risk decisions remain server-side. The system remains paper-trading only; no live broker execution is enabled.


## Global emergency-stop permissions

The global emergency-stop and resume endpoints require an authenticated user whose
database role is `ADMIN` and whose email is marked verified. The backend checks
the current database record on each request; a stale client role or email address
cannot grant access. Public registration cannot create administrator accounts.
The first-admin provisioning and verified-email delivery workflows remain
prerequisites and are not yet available. Until then these controls fail closed.


## Backend browser-origin configuration

For a separately hosted frontend, configure `TAIMOBOT_CORS_ORIGINS` on the
backend as a comma-separated list of exact trusted HTTP(S) origins, for example
`https://dashboard.example.com`. Wildcards and URL paths are rejected. The
allowlist is empty by default, cross-origin credentials are disabled, and the
local Vite proxy continues to work without this setting.


## Strategy knowledge

The authenticated Strategy Knowledge screen/API foundation exposes built-in reference
techniques and the signed-in user's own notes. New contributions remain explicitly
unvalidated and cannot be used as live trading signals. Strategy backtesting,
combination, autonomous strategy generation and decision explanations are later
milestones; they are not implied by the existence of the library.
