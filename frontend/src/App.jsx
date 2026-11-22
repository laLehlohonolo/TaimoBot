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
      const justRegistered = mode === "register";
      if (justRegistered) {
        await apiRequest("/auth/register", {
          method: "POST",
          body: JSON.stringify({ email, password })
        });
      }
      const session = await apiRequest("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password })
      });
      onAuthenticated(session, justRegistered);
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


function StrategyLibrary({ token }) {
  const [strategies, setStrategies] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [form, setForm] = useState({
    name: "", category: "SCALPING", description: "",
    entry_logic: "", exit_logic: "", risk_notes: ""
  });

  const loadStrategies = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await apiRequest("/strategies", {}, token);
      setStrategies(result.strategies || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load strategy knowledge.");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { loadStrategies(); }, [loadStrategies]);

  async function submit(event) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setNotice("");
    try {
      await apiRequest("/strategies", {
        method: "POST",
        body: JSON.stringify(form)
      }, token);
      setForm({
        name: "", category: "SCALPING", description: "",
        entry_logic: "", exit_logic: "", risk_notes: ""
      });
      setNotice("Research note saved. It remains unvalidated and cannot trigger trades.");
      await loadStrategies();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save strategy note.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="panel strategy-library">
      <div className="strategy-heading">
        <div>
          <p className="eyebrow">LEARNING / KNOWLEDGE BASE</p>
          <h2>Strategy knowledge</h2>
          <p className="muted">A growing library of techniques and research notes. Every entry starts unvalidated.</p>
        </div>
        <span className="strategy-count">{strategies.length} techniques</span>
      </div>
      {error && <div className="notice error" role="alert">{error}</div>}
      {notice && <div className="notice success" role="status">{notice}</div>}
      {loading ? <p className="muted">Loading strategy knowledge…</p> : (
        <div className="strategy-list">
          {strategies.map(item => (
            <details className="strategy-item" key={item.strategy_id}>
              <summary>
                <span className="strategy-name">{item.name}<small>{item.category.replaceAll("_", " ")}</small></span>
                <span className="strategy-meta">
                  <span className="strategy-status">{item.evidence_status.replaceAll("_", " ")}</span>
                  <span className="strategy-source">{item.owner_user_id ? "Your research" : "Reference technique"}</span>
                </span>
              </summary>
              <div className="strategy-detail">
                <p>{item.description}</p>
                <div><strong>Entry approach</strong><p>{item.entry_logic}</p></div>
                <div><strong>Exit approach</strong><p>{item.exit_logic}</p></div>
                <div><strong>Risk considerations</strong><p>{item.risk_notes}</p></div>
                <small>Evidence: {item.evidence_status.replaceAll("_", " ")} · Source: {item.provenance.replaceAll("_", " ")}</small>
              </div>
            </details>
          ))}
        </div>
      )}
      <form className="strategy-form" onSubmit={submit}>
        <div>
          <p className="eyebrow">CONTRIBUTE KNOWLEDGE</p>
          <h3>Add a strategy or technique</h3>
          <p className="muted">Record the method and its rules. All new notes remain unvalidated until independently tested.</p>
        </div>
        <label>Strategy name<input required minLength={3} maxLength={100} value={form.name} onChange={event => setForm(current => ({ ...current, name: event.target.value }))} placeholder="e.g. London session pullback" /></label>
        <label>Category<select value={form.category} onChange={event => setForm(current => ({ ...current, category: event.target.value }))}>
          <option value="SCALPING">Scalping</option><option value="TREND">Trend</option><option value="MOMENTUM">Momentum</option><option value="SESSION">Session</option><option value="LEVELS">Support / resistance</option><option value="RANGE">Mean reversion / range</option><option value="MULTI_TIMEFRAME">Multi-timeframe</option><option value="OTHER">Other</option>
        </select></label>
        <label>Description<textarea required minLength={10} maxLength={1000} value={form.description} onChange={event => setForm(current => ({ ...current, description: event.target.value }))} rows={2} /></label>
        <label>Entry rules<textarea required minLength={10} maxLength={2000} value={form.entry_logic} onChange={event => setForm(current => ({ ...current, entry_logic: event.target.value }))} rows={3} /></label>
        <label>Exit rules<textarea required minLength={10} maxLength={2000} value={form.exit_logic} onChange={event => setForm(current => ({ ...current, exit_logic: event.target.value }))} rows={3} /></label>
        <label>Risk considerations<textarea required minLength={10} maxLength={2000} value={form.risk_notes} onChange={event => setForm(current => ({ ...current, risk_notes: event.target.value }))} rows={2} /></label>
        <button className="primary-button" type="submit" disabled={saving}>{saving ? "Saving…" : "Save research note"}</button>
      </form>
      <p className="strategy-disclaimer">Reference techniques and research notes are educational records, not trading recommendations or proven profitable systems. Strategy synthesis and automated execution are not enabled.</p>
    </section>
  );
}

function WelcomeCelebration({ onClose }) {
  return (
    <div className="welcome-overlay" role="dialog" aria-modal="true" aria-labelledby="welcome-title">
      <div className="welcome-confetti" aria-hidden="true"><span>✦</span><span>✧</span><span>✦</span><span>✧</span><span>✦</span><span>✧</span></div>
      <div className="welcome-card">
        <div className="welcome-emblem" aria-hidden="true">T</div>
        <p className="eyebrow">A NEW JOURNEY BEGINS</p>
        <h2 id="welcome-title">Welcome to <em>TaimoBot.</em></h2>
        <p>Your place in the TaimoBot community starts today. Your journey, milestones and trading history will grow with you.</p>
        <div className="welcome-signature">WELCOME ABOARD · LET'S BUILD YOUR LEGACY</div>
        <button className="primary-button" type="button" onClick={onClose}>Enter your workspace</button>
      </div>
    </div>
  );
}

function CurrencyPreferencePanel({ token }) {
  const [preference, setPreference] = useState(null);
  const [currency, setCurrency] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    apiRequest("/participant/currency-preference", {}, token)
      .then(result => { if (active) setPreference(result); })
      .catch(err => { if (active) setError(err instanceof Error ? err.message : "Could not load your currency preference."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [token]);

  async function submit(event) {
    event.preventDefault();
    if (!currency || !confirmed || saving) return;
    setSaving(true);
    setError("");
    try {
      const result = await apiRequest("/participant/currency-preference", {
        method: "POST",
        body: JSON.stringify({ currency, confirmed: true })
      }, token);
      setPreference(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Currency preference could not be saved.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="panel currency-preference-panel">
      <p className="eyebrow">PARTICIPANT DISPLAY PREFERENCE</p>
      <h2>Choose your currency once</h2>
      {loading ? <p className="muted">Checking your saved preference…</p> : preference?.selected ? (
        <div role="status">
          <p>Your permanent display preference is <strong>{preference.currency}</strong>.</p>
          <p className="muted">Selected {new Date(preference.selected_at).toLocaleString("en-ZA")}. This choice is locked and cannot be changed.</p>
        </div>
      ) : (
        <form onSubmit={submit} className="auth-form">
          <p className="muted">Select one currency: South African Rand, US Dollar, Euro, Pound Sterling or Kuwaiti Dinar.</p>
          <label>Preferred currency
            <select required value={currency} onChange={event => setCurrency(event.target.value)}>
              <option value="">Choose a currency</option>
              <option value="ZAR">ZAR — South African Rand</option>
              <option value="USD">USD — US Dollar</option>
              <option value="EUR">EUR — Euro</option>
              <option value="GBP">GBP — Pound Sterling</option>
              <option value="KWD">KWD — Kuwaiti Dinar</option>
            </select>
          </label>
          <label className="currency-confirmation">
            <input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />
            I understand this is a once-off choice and cannot be changed later.
          </label>
          {error && <div className="notice error" role="alert">{error}</div>}
          <button className="primary-button" type="submit" disabled={!currency || !confirmed || saving}>
            {saving ? "Saving preference…" : "Confirm permanent choice"}
          </button>
        </form>
      )}
      {error && preference?.selected && <div className="notice error" role="alert">{error}</div>}
      <p className="muted">Currency selection changes display preference only; it does not move, convert, or relabel funds. Each paper account is labelled using its own persisted settlement currency. Legacy accounts without a verified denomination remain marked currency unverified.</p>
    </section>
  );
}

function ParticipantJourney({ timeline }) {
  const formatDate = value => value ? new Date(value.replace(" ", "T") + (value.endsWith("Z") ? "" : "Z")).toLocaleDateString("en-ZA", { day: "numeric", month: "long", year: "numeric" }) : "Not yet recorded";
  return (
    <section className="panel participant-journey">
      <div><p className="eyebrow">YOUR TAIMOBOT STORY</p><h2>Participant journey</h2><p className="muted">Your joining milestone is the start of your 10-year loyalty journey. Trading milestones are tracked separately.</p></div>
      <div className="journey-grid">
        <article><span className="journey-icon">✦</span><small>JOINED TAIMOBOT</small><strong>{formatDate(timeline.joined_at)}</strong><span>10-year loyalty clock starts here</span></article>
        <article><span className="journey-icon">↗</span><small>FIRST SUCCESSFUL TRADE</small><strong>{formatDate(timeline.first_trade_at)}</strong><span>{timeline.first_trade_at ? "Your trading history has begun" : "Recorded when your first paper trade opens"}</span></article>
        <article><span className="journey-icon">🏆</span><small>10-YEAR RECOGNITION</small><strong>Coming in your tenth year</strong><span>Recognition confirmed; reward to be decided in the future</span></article>
      </div>
    </section>
  );
}

export default function App() {
  const [health, setHealth] = useState(null);
  const [safety, setSafety] = useState(null);
  const [session, setSession] = useState(null);
  const [showWelcome, setShowWelcome] = useState(false);
  const [participantTimeline, setParticipantTimeline] = useState(null);
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
  const [orderSymbol, setOrderSymbol] = useState("XAUUSD");
  const [orderSide, setOrderSide] = useState("BUY");
  const [entryPrice, setEntryPrice] = useState("");
  const [stopLoss, setStopLoss] = useState("");
  const [takeProfit, setTakeProfit] = useState("");
  const [orderBusy, setOrderBusy] = useState(false);
  const [orderPreview, setOrderPreview] = useState(null);
  const [closeBusy, setCloseBusy] = useState("");

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
    if (!session?.access_token) {
      setParticipantTimeline(null);
      return;
    }
    let active = true;
    apiRequest("/auth/participant/timeline", {}, session.access_token)
      .then(result => { if (active) setParticipantTimeline(result); })
      .catch(() => { if (active) setParticipantTimeline(null); });
    return () => { active = false; };
  }, [session]);
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
          starting_balance: Number(startingBalance),
          settlement_currency: "ZAR"
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

  async function previewPaperOrder(event) {
    event.preventDefault();
    if (!session?.access_token || !account || !risk) return;
    setError("");
    setOrderPreview(null);
    setOrderBusy(true);
    try {
      const result = await apiRequest("/accounts/" + encodeURIComponent(account.account_id) + "/risk/preview", {
        method: "POST",
        body: JSON.stringify({
          entry_price: Number(entryPrice),
          stop_loss: Number(stopLoss),
          side: orderSide
        })
      }, session.access_token);
      setOrderPreview({
        ...result,
        assignedRisk: Number(result.assigned_risk_percent),
        projectedRisk: Number(result.projected_total_open_risk)
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Risk preview failed.");
    } finally {
      setOrderBusy(false);
    }
  }

  async function submitPaperOrder(event) {
    event.preventDefault();
    if (!session?.access_token || !account) return;
    setOrderBusy(true);
    setError("");
    try {
      await apiRequest("/trades", {
        method: "POST",
        body: JSON.stringify({
          account_id: account.account_id,
          client_order_id: "WEB-" + crypto.randomUUID(),
          symbol: orderSymbol.trim().toUpperCase(),
          side: orderSide,
          entry_price: Number(entryPrice),
          stop_loss: Number(stopLoss),
          take_profit: takeProfit.trim() ? Number(takeProfit) : null,
          risk_percent: orderPreview?.assignedRisk ?? 1.0
        })
      }, session.access_token);
      setEntryPrice("");
      setStopLoss("");
      setTakeProfit("");
      setOrderPreview(null);
      await loadSelectedAccount(session.access_token, account.account_id);
      await loadAccounts(session.access_token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Paper order was rejected.");
    } finally {
      setOrderBusy(false);
    }
  }

  async function closePaperTrade(trade) {
    if (!session?.access_token || trade.status !== "OPEN") return;
    const raw = window.prompt("Enter simulated exit price for " + trade.symbol + ":");
    if (raw === null) return;
    const exitPrice = Number(raw);
    if (!Number.isFinite(exitPrice) || exitPrice <= 0) {
      setError("Exit price must be a valid positive number.");
      return;
    }
    setCloseBusy(trade.trade_id);
    setError("");
    try {
      await apiRequest("/trades/" + encodeURIComponent(trade.trade_id) + "/close", {
        method: "POST",
        body: JSON.stringify({ exit_price: exitPrice })
      }, session.access_token);
      await loadSelectedAccount(session.access_token, account.account_id);
      await loadAccounts(session.access_token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Paper close failed.");
    } finally {
      setCloseBusy("");
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
        <AuthPanel onAuthenticated={(nextSession, justRegistered) => { setSession(nextSession); setShowWelcome(Boolean(justRegistered)); }} />
      ) : (
        <section className="workspace">
          <div className="workspace-heading">
            <div><p className="eyebrow">YOUR WORKSPACE</p><h2>Paper accounts</h2><p className="muted">Signed in as {session.user?.email || "authenticated user"}</p></div>
            <StatusPill good={true}>Authenticated</StatusPill>
          </div>

          {participantTimeline && <ParticipantJourney timeline={participantTimeline} />}
          <CurrencyPreferencePanel token={session.access_token} />
          <div className="account-layout">
            <aside className="panel account-sidebar">
              <h3>Your accounts</h3>
              {accounts.length === 0 ? <p className="muted">No paper accounts yet. Create one below.</p> : accounts.map(item => (
                <button key={item.account_id} className={"account-choice " + (selectedId === item.account_id ? "selected" : "")} onClick={() => setSelectedId(item.account_id)}>
                  <span>{item.account_id}</span><strong>{item.settlement_currency ? item.settlement_currency + " " : "Unverified · "}{Number(item.balance).toLocaleString("en-ZA", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong>
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
                    <MetricCard label="CURRENT BALANCE" value={(account.settlement_currency || "Unverified") + " " + Number(account.current_balance).toLocaleString("en-ZA", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} note={"Starting balance " + (account.settlement_currency || "currency unverified") + " " + Number(account.starting_balance).toLocaleString("en-ZA")} />
                    <MetricCard label="OPEN POSITIONS" value={account.open_trades} note={account.total_trades + " total trades"} />
                    <MetricCard label="REALIZED P&L" value={(account.settlement_currency || "Unverified") + " " + Number(account.realized_pnl).toFixed(2)} note={account.closed_trades + " closed trades"} />
                  </div>
                  <div className="panel order-panel">
                    <div className="table-heading"><div><p className="eyebrow">SIMULATED EXECUTION</p><h3>Open paper position</h3></div><StatusPill good={!emergencyActive && safety?.new_orders_allowed !== false}>{emergencyActive || safety?.new_orders_allowed === false ? "Entries blocked" : "Paper only"}</StatusPill></div>
                    <form className="order-form" onSubmit={submitPaperOrder}>
                      <label>Instrument<input required minLength={1} maxLength={30} pattern="[A-Za-z0-9._/-]+" value={orderSymbol} onChange={event => { setOrderSymbol(event.target.value); setOrderPreview(null); }} /></label>
                      <label>Direction<select value={orderSide} onChange={event => { setOrderSide(event.target.value); setOrderPreview(null); }}><option value="BUY">BUY</option><option value="SELL">SELL</option></select></label>
                      <label>Entry price<input required type="number" min="0.00000001" step="any" value={entryPrice} onChange={event => { setEntryPrice(event.target.value); setOrderPreview(null); }} /></label>
                      <label>Stop loss<input required type="number" min="0.00000001" step="any" value={stopLoss} onChange={event => { setStopLoss(event.target.value); setOrderPreview(null); }} /></label>
                      <label>Take profit (optional)<input type="number" min="0.00000001" step="any" value={takeProfit} onChange={event => setTakeProfit(event.target.value)} /></label>
                      <div className="order-actions"><button className="secondary-button" type="button" disabled={orderBusy || !entryPrice || !stopLoss} onClick={previewPaperOrder}>{orderBusy ? "Checking…" : "Preview risk"}</button><button className="primary-button" type="submit" disabled={orderBusy || !entryPrice || !stopLoss || !risk || emergencyActive || safety?.new_orders_allowed === false}>{orderBusy ? "Submitting…" : "Open paper position"}</button></div>
                    </form>
                    {orderPreview && <div className="preview-result" role="status"><strong>Estimated position size: {orderPreview.position_size}</strong><span>Risk amount: {account.settlement_currency || "Unverified"} {Number(orderPreview.risk_amount).toFixed(2)} · Assigned profile: {orderPreview.assignedRisk}%</span><small>Preview is indicative. The backend recalculates assigned risk and checks combined account exposure before accepting the order.</small></div>}
                    <p className="muted">Paper execution only. The backend enforces your assigned risk profile and the combined 1% account ceiling. Existing positions are never closed automatically by this form.</p>
                  </div>
                  {risk && <div className="panel risk-panel"><div><span className="card-label">OPEN RISK</span><strong>{account.settlement_currency || "Unverified"} {Number(risk.current_open_risk).toFixed(2)} / {account.settlement_currency || "Unverified"} {Number(risk.maximum_risk_amount).toFixed(2)}</strong></div><div className="risk-track"><span style={{ width: Math.min(100, risk.maximum_risk_amount ? (risk.current_open_risk / risk.maximum_risk_amount) * 100 : 0) + "%" }} /></div><p className="muted">Maximum combined account risk: {risk.maximum_risk_percent}% · remaining budget {account.settlement_currency || "Unverified"} {Number(risk.remaining_risk_budget).toFixed(2)}</p></div>}
                  <div className="panel trades-panel"><div className="table-heading"><h3>Recent trades</h3><span className="muted">{trades.length} records</span></div>
                    {trades.length === 0 ? <p className="muted">No trades recorded for this account.</p> : <div className="table-wrap"><table><thead><tr><th>Instrument</th><th>Side</th><th>Entry</th><th>Stop loss</th><th>Risk</th><th>Status</th><th>Realized P&L</th><th>Actions</th></tr></thead><tbody>{trades.map(trade => <tr key={trade.trade_id}><td>{trade.symbol}</td><td>{trade.side}</td><td>{Number(trade.entry_price).toFixed(2)}</td><td>{Number(trade.stop_loss).toFixed(2)}</td><td>{account.settlement_currency || "Unverified"} {Number(trade.risk_amount).toFixed(2)}</td><td><span className={"trade-status " + trade.status.toLowerCase()}>{trade.status}</span></td><td>{account.settlement_currency || "Unverified"} {Number(trade.realized_pnl).toFixed(2)}</td><td>{trade.status === "OPEN" && <button className="text-button" disabled={closeBusy === trade.trade_id} onClick={() => closePaperTrade(trade)}>{closeBusy === trade.trade_id ? "Closing…" : "Close paper"}</button>}</td></tr>)}</tbody></table></div>}
                  </div>
                </>
              ) : <div className="panel empty-state"><h2>{accounts.length ? "Select an account" : "Your paper workspace is ready"}</h2><p className="muted">{accounts.length ? "Choose an account to inspect its balance, risk and trade history." : "Create your first paper account to begin tracking simulated activity."}</p></div>}
            </div>
          </div>
          <StrategyLibrary token={session.access_token} />
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

      {showWelcome && <WelcomeCelebration onClose={() => setShowWelcome(false)} />}
      <footer><span>TAIMOBOT · BUILD 0.2.0</span><span>{lastChecked ? "Last checked " + lastChecked.toLocaleTimeString() : "Awaiting status check"} · Africa/Johannesburg · Session is memory-only</span></footer>
    </main>
  );
}
