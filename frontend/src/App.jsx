import { useCallback, useEffect, useState } from "react";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/$/, "");

async function apiGet(path) {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "GET",
    headers: { Accept: "application/json" },
    credentials: "omit",
    cache: "no-store"
  });
  if (!response.ok) throw new Error(`API returned HTTP ${response.status}`);
  return response.json();
}

function StatusPill({ good, children }) {
  return <span className={`pill ${good ? "good" : "warn"}`}><i />{children}</span>;
}

export default function App() {
  const [health, setHealth] = useState(null);
  const [safety, setSafety] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [lastChecked, setLastChecked] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [healthResult, safetyResult] = await Promise.all([
        apiGet("/health"),
        apiGet("/safety/status")
      ]);
      setHealth(healthResult);
      setSafety(safetyResult);
      setLastChecked(new Date());
    } catch (err) {
      setHealth(null);
      setSafety(null);
      setError(err instanceof Error ? err.message : "Unable to reach TaimoBot API");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const connected = Boolean(health?.status === "healthy");
  const emergencyActive = Boolean(safety?.emergency_stop);

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="TaimoBot home">
          <span className="brand-mark">T</span>
          <span>TAIMO<span className="brand-light">BOT</span></span>
        </a>
        <div className="topbar-right">
          <span className="mode-label">PAPER ENVIRONMENT</span>
          <StatusPill good={connected}>{loading ? "Checking API" : connected ? "API connected" : "API offline"}</StatusPill>
        </div>
      </header>

      <section className="intro">
        <div>
          <p className="eyebrow">CONTROL CENTRE / OVERVIEW</p>
          <h1>Trade with <em>discipline.</em></h1>
          <p className="subhead">A controlled workspace for testing strategies and managing paper-trading risk.</p>
        </div>
        <button className="refresh" onClick={refresh} disabled={loading}>
          {loading ? "Checking…" : "↻ Refresh status"}
        </button>
      </section>

      {error && <div className="notice error" role="alert">
        <strong>Backend connection unavailable.</strong>
        <span>{error}. Confirm that the FastAPI service is running and the API base URL is correct.</span>
      </div>}

      <section className="status-grid" aria-label="System status">
        <article className="panel status-card">
          <div className="card-top"><span className="card-label">API HEALTH</span><span className="signal">01</span></div>
          <div className="metric">{connected ? "Online" : loading ? "…" : "Offline"}</div>
          <p>{health?.api || "Waiting for backend response"}</p>
          <StatusPill good={connected}>{connected ? "Healthy" : "Not connected"}</StatusPill>
        </article>
        <article className="panel status-card">
          <div className="card-top"><span className="card-label">TRADING MODE</span><span className="signal">02</span></div>
          <div className="metric">Paper</div>
          <p>Simulated execution only</p>
          <StatusPill good={true}>No live orders</StatusPill>
        </article>
        <article className="panel status-card">
          <div className="card-top"><span className="card-label">EMERGENCY STOP</span><span className="signal">03</span></div>
          <div className="metric">{safety ? (emergencyActive ? "Active" : "Inactive") : "—"}</div>
          <p>{safety ? (emergencyActive ? "New orders are blocked by the backend." : "Backend reports new orders are permitted.") : "Safety state not yet verified"}</p>
          <StatusPill good={Boolean(safety) && emergencyActive}>{safety ? (emergencyActive ? "Protection engaged" : "Not engaged") : "Unknown"}</StatusPill>
        </article>
      </section>

      <section className="panel integrity">
        <div>
          <p className="eyebrow">SAFETY FIRST</p>
          <h2>Backend-enforced controls</h2>
          <p className="integrity-copy">Risk validation, order acceptance and emergency-stop enforcement belong to the API. The browser is a display layer—not a security boundary.</p>
        </div>
        <div className="guard-list">
          <div><span className="check">✓</span><span>Paper trading only</span></div>
          <div><span className="check">✓</span><span>Maximum risk enforced by backend</span></div>
          <div><span className="check">✓</span><span>Emergency-stop state read from API</span></div>
          <div><span className="check">✓</span><span>No broker credentials in browser</span></div>
        </div>
      </section>

      <footer>
        <span>TAIMOBOT · BUILD 0.1.0</span>
        <span>{lastChecked ? `Last checked ${lastChecked.toLocaleTimeString()}` : "Awaiting first status check"} · Africa/Johannesburg</span>
      </footer>
    </main>
  );
}
