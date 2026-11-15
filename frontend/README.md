# TaimoBot frontend

React + Vite dashboard for the TaimoBot FastAPI paper-trading backend.

## Local development

1. Start the backend from the repository's `backend` directory:
   `uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`
2. In a second terminal, enter `frontend`, install dependencies with `npm install`, then run `npm run dev`.
3. Open the Vite address shown in the terminal. The development server proxies `/api` requests to the local FastAPI service.

## Configuration

Set `VITE_API_BASE_URL` to the API base path or URL. The default is `/api`.

**Security boundary:** VITE variables are compiled into browser-visible assets. Never put API keys, passwords, signing secrets, broker credentials, or private tokens in them. The current dashboard is intentionally read-only. The backend API currently has no user authentication, so do not expose it to the public internet or add order/control actions to a public frontend until authentication and authorization are implemented server-side.

The UI reports backend health and emergency-stop status. All order and risk decisions remain server-side.
