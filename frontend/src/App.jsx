import { useCallback, useEffect, useState } from "react";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/$/, "");

async function apiRequest(path, options = {}, token = null) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = "Bearer " + token;
  if (options.body) headers["Content-Type"] = "application/json";

  const response = await fetch(API_BASE + path, {
    ...options,
    headers,
    credentials: "omit",
    cache: "no-store"
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || ("API returned HTTP " + response.status));
  }
  return body;
}

function StatusPill({ good, children }) {
  return <span className={"pill " + (good ? "good" : "warn")}><i />{children}</span>;
}

function AuthPanel({ onAuthenticated }) {
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (mode === "register") {
        await apiRequest("/auth/register", {
          method: "POST",
          body: JSON.stringify({ email, password })
        });
      }
      const session = await apiRequest("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password })
      });
      onAuthenticated(session);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Authentication failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel auth-panel">
      <p className="eyebrow">SECURE ACCESS</p>
      <h2>{mode === "login" ? "Welcome back." : "Create your account."}</h2>
      <p className="muted">Sign in to view your own paper accounts. Your session stays in memory and is cleared when this page is refreshed.</p>
      <form onSubmit={submit} className="auth-form">
        <label>Email address<input type="email" autoComplete="email" required maxLength={254} value={email} onChange={event => setEmail(event.target.value)} /></label>
        <label>Password<input type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} required minLength={mode === "login" ? 1 : 12} maxLength={1024} value={password} onChange={event => setPassword(event.target.value)} /></label>
        {error && <div className="notice error" role="alert">{error}</div>}
        <button className="primary-button" type="submit" disabled={busy}>{busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}</button>
      </form>
      <button className="text-button" type="button" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(""); }}>
        {mode === "login" ? "New to TaimoBot? Create an account" : "Already registered? Sign in"}
      </button>
    </section>
  );
}

function MetricCard({ label, value, note }) {
  return <article className="panel metric-card"><span className="card-label">{label}</span><div className="metric">{value}</div><p>{note}</p></article>;
}

export default function App() {
  const [health, setHealth] = useState(null);
  const [safety, setSafety] = useState(null);
  const [session, setSession] = useState(null);
  const [accounts, setAccounts] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [account, setAccount] = useState(null);
  const [risk, setRisk] = useState(null);
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [lastChecked, setLastChecked] = useState(null);
  const [newAccountId, setNewAccountId] = useState("");
  const [startingBalance, setStartingBalance] = useState("10000");
  const [creating, setCreating] = useState(false);

  const refreshPublicStatus = useCallback(async () => {
    try {
      const [healthResult, safetyResult] = await Promise.all([
        apiRequest("/health"),
        apiRequest("/safety/status")
      ]);
      setHealth(healthResult);
      setSafety(safetyResult);
      setLastChecked(new Date());
    } catch {
      setHealth(null);
      setSafety(null);
    }
  }, []);

  const loadAccounts = useCallback(async (token) => {
    setLoading(true);
    setError("");
    try {
      const result = await apiRequest("/accounts", {}, token);
      setAccounts(result.accounts || []);
      setSelectedId(current => {
        if (current && result.accounts.some(item => item.account_id === current)) return current;
        return result.accounts[0]?.account_id || "";
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load accounts.");
      setAccounts([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadSelectedAccount = useCallback(async (token, accountId) => {
    if (!token || !accountId) {
      setAccount(null);
      setRisk(null);
      setTrades([]);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const [accountResult, riskResult, tradeResult] = await Promise.all([
        apiRequest("/accounts/" + encodeURIComponent(accountId), {}, token),
        apiRequest("/accounts/" + encodeURIComponent(accountId) + "/risk", {}, token),
        apiRequest("/accounts/" + encodeURIComponent(accountId) + "/trades", {}, token)
      ]);
      setAccount(accountResult);
      setRisk(riskResult);
      setTrades(tradeResult.trades || []);
      setLastChecked(new Date());
    } catch (err) {
      setAccount(null);
      setRisk(null);
      setTrades([]);
      setError(err instanceof Error ? err.message : "Could not load account details.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refreshPublicStatus(); }, [refreshPublicStatus]);
  useEffect(() => {
    if (session?.access_token) loadAccounts(session.access_token);
    else {
      setAccounts([]);
      setSelectedId("");
      setAccount(null);
      setRisk(null);
      setTrades([]);
    }
  }, [session, loadAccounts]);
  useEffect(() => {
    if (session?.access_token && selectedId) loadSelectedAccount(session.access_token, selectedId);
    else {
      setAccount(null);
      setRisk(null);
      setTrades([]);
    }
  }, [session, selectedId, loadSelectedAccount]);

  async function createAccount(event) {
    event.preventDefault();
    if (!session?.access_token) return;
    setCreating(true);
    setError("");
    try {
      await apiRequest("/accounts", {
        method: "POST",
        body: JSON.stringify({
          account_id: newAccountId.trim(),
          starting_balance: Number(startingBalance)
        })
      }, session.access_token);
      setNewAccountId("");
      await loadAccounts(session.access_token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create paper account.");
    } finally {
      setCreating(false);
    }
  }

  async function logout() {
    if (session?.access_token) {
      try { await apiRequest("/auth/logout", { method: "POST" }, session.access_token); } catch { /* clear local session regardless */ }
    }
    setSession(null);
  }

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
          <StatusPill good={connected}>{connected ? "API connected" : "API unavailable"}</StatusPill>
          {session && <button className="text-button logout" onClick={logout}>Sign out</button>}
        </div>
      </header>

      <section className="intro">
        <div>
          <p className="eyebrow">CONTROL CENTRE / OVERVIEW</p>
          <h1>Trade with <em>discipline.</em></h1>
          <p className="subhead">A controlled workspace for testing strategies and managing paper-trading risk.</p>
        </div>
        <button className="refresh" onClick={async () => { await refreshPublicStatus(); if (session?.access_token) await loadAccounts(session.access_token); }} disabled={loading}>
          {loading ? "Refreshing…" : "↻ Refresh status"}
        </button>
      </section>

      <section className="status-grid" aria-label="System status">
        <MetricCard label="API HEALTH" value={connected ? "Online" : "Offline"} note={health?.api || "Waiting for backend response"} />
        <MetricCard label="TRADING MODE" value="Paper" note="Simulated execution only · no live orders" />
        <MetricCard label="EMERGENCY STOP" value={safety ? (emergencyActive ? "Active" : "Inactive") : "Unknown"} note={safety ? (emergencyActive ? "New orders are blocked by the backend." : "Backend reports new orders are permitted.") : "Safety state not verified"} />
      </section>

      {error && <div className="notice error" role="alert"><strong>Workspace notice</strong><span>{error}</span></div>}

      {!session ? (
        <AuthPanel onAuthenticated={setSession} />
      ) : (
        <section className="workspace">
          <div className="workspace-heading">
            <div><p className="eyebrow">YOUR WORKSPACE</p><h2>Paper accounts</h2><p className="muted">Signed in as {session.user?.email || "authenticated user"}</p></div>
            <StatusPill good={true}>Authenticated</StatusPill>
          </div>

          <div className="account-layout">
            <aside className="panel account-sidebar">
              <h3>Your accounts</h3>
              {accounts.length === 0 ? <p className="muted">No paper accounts yet. Create one below.</p> : accounts.map(item => (
                <button key={item.account_id} className={"account-choice " + (selectedId === item.account_id ? "selected" : "")} onClick={() => setSelectedId(item.account_id)}>
                  <span>{item.account_id}</span><strong>R {Number(item.balance).toLocaleString("en-ZA", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong>
                </button>
              ))}
              <form className="create-account-form" onSubmit={createAccount}>
                <h3>Create paper account</h3>
                <label>Account ID<input required minLength={1} maxLength={100} value={newAccountId} onChange={event => setNewAccountId(event.target.value)} placeholder="e.g. TAIMO-001" /></label>
                <label>Starting balance (ZAR)<input required type="number" min="1" step="0.01" value={startingBalance} onChange={event => setStartingBalance(event.target.value)} /></label>
                <button className="primary-button" disabled={creating}>{creating ? "Creating…" : "Create account"}</button>
              </form>
            </aside>

            <div className="account-main">
              {account ? (
                <>
                  <div className="workspace-heading"><div><p className="eyebrow">ACCOUNT / {account.account_id}</p><h2>Account overview</h2></div><StatusPill good={true}>Paper trading</StatusPill></div>
                  <div className="status-grid account-metrics">
                    <MetricCard label="CURRENT BALANCE" value={"R " + Number(account.current_balance).toLocaleString("en-ZA", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} note={"Starting balance R " + Number(account.starting_balance).toLocaleString("en-ZA")} />
                    <MetricCard label="OPEN POSITIONS" value={account.open_trades} note={account.total_trades + " total trades"} />
                    <MetricCard label="REALIZED P&L" value={"R " + Number(account.realized_pnl).toFixed(2)} note={account.closed_trades + " closed trades"} />
                  </div>
                  {risk && <div className="panel risk-panel"><div><span className="card-label">OPEN RISK</span><strong>R {Number(risk.current_open_risk).toFixed(2)} / R {Number(risk.maximum_risk_amount).toFixed(2)}</strong></div><div className="risk-track"><span style={{ width: Math.min(100, risk.maximum_risk_amount ? (risk.current_open_risk / risk.maximum_risk_amount) * 100 : 0) + "%" }} /></div><p className="muted">Maximum combined account risk: {risk.maximum_risk_percent}% · remaining budget R {Number(risk.remaining_risk_budget).toFixed(2)}</p></div>}
                  <div className="panel trades-panel"><div className="table-heading"><h3>Recent trades</h3><span className="muted">{trades.length} records</span></div>
                    {trades.length === 0 ? <p className="muted">No trades recorded for this account.</p> : <div className="table-wrap"><table><thead><tr><th>Instrument</th><th>Side</th><th>Entry</th><th>Stop loss</th><th>Risk</th><th>Status</th><th>Realized P&L</th></tr></thead><tbody>{trades.map(trade => <tr key={trade.trade_id}><td>{trade.symbol}</td><td>{trade.side}</td><td>{Number(trade.entry_price).toFixed(2)}</td><td>{Number(trade.stop_loss).toFixed(2)}</td><td>R {Number(trade.risk_amount).toFixed(2)}</td><td><span className={"trade-status " + trade.status.toLowerCase()}>{trade.status}</span></td><td>R {Number(trade.realized_pnl).toFixed(2)}</td></tr>)}</tbody></table></div>}
                  </div>
                </>
              ) : <div className="panel empty-state"><h2>{accounts.length ? "Select an account" : "Your paper workspace is ready"}</h2><p className="muted">{accounts.length ? "Choose an account to inspect its balance, risk and trade history." : "Create your first paper account to begin tracking simulated activity."}</p></div>}
            </div>
          </div>
        </section>
      )}

      <section className="panel integrity">
        <div><p className="eyebrow">SAFETY FIRST</p><h2>Backend-enforced controls</h2><p className="integrity-copy">Authentication, account ownership, risk validation and order acceptance are enforced by the API. This browser dashboard is a display layer and does not place orders.</p></div>
        <div className="guard-list">
          <div><span className="check">✓</span><span>Paper trading only</span></div>
          <div><span className="check">✓</span><span>Private accounts require authentication</span></div>
          <div><span className="check">✓</span><span>Maximum risk enforced by backend</span></div>
          <div><span className="check">✓</span><span>No broker credentials in browser</span></div>
        </div>
      </section>

      <footer><span>TAIMOBOT · BUILD 0.2.0</span><span>{lastChecked ? "Last checked " + lastChecked.toLocaleTimeString() : "Awaiting status check"} · Africa/Johannesburg · Session is memory-only</span></footer>
    </main>
  );
}
