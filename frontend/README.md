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
- Health and emergency-stop status are public read-only endpoints. Emergency-stop activation and resume require authentication.

**Deployment warning:** the backend currently has no administrator role or user-management workflow. Do not expose emergency-stop controls to untrusted users, and do not expose the backend publicly until HTTPS, production CORS/origin restrictions, secret management, backups, monitoring and deployment hardening have been configured. Never place API keys, signing secrets, passwords or broker credentials in `VITE_*` variables; these are visible in browser assets.

All trading and risk decisions remain server-side. The system remains paper-trading only; no live broker execution is enabled.


## Global emergency-stop permissions

The global emergency-stop and resume endpoints are administrator-only. The backend
must be configured with `TAIMOBOT_ADMIN_EMAILS` (a comma-separated allowlist of
administrator email addresses). If the variable is empty or unset, these controls
fail closed and return HTTP 403 to regular users. Never put this setting in frontend
environment variables or browser code.
