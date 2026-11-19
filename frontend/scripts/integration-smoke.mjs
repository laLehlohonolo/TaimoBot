const API = "http://127.0.0.1:4173/api";

async function waitForApi() {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      const response = await fetch(API + "/health");
      if (response.ok) return;
    } catch {
      // The backend or Vite proxy may still be starting.
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error("API did not become ready through the Vite proxy.");
}

async function request(path, { method = "GET", body, token } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = "Bearer " + token;
  const response = await fetch(API + path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store"
  });
  const payload = await response.json().catch(() => ({}));
  return { response, payload };
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

await waitForApi();

const health = await request("/health");
assert(health.response.status === 200 && health.payload.status === "healthy", "Health check failed.");
console.log("PASS: Vite proxy reaches the live FastAPI health endpoint.");

const anonymous = await request("/accounts");
assert(anonymous.response.status === 401, "Private account list was not protected.");
console.log("PASS: Anonymous account access is rejected.");

const unique = Date.now().toString(36);
const email = "integration-" + unique + "@taimobot.test";
const password = "TaimoBot-Integration-2026";

const registration = await request("/auth/register", {
  method: "POST",
  body: { email, password }
});
assert(registration.response.status === 200, "Registration failed: " + JSON.stringify(registration.payload));
console.log("PASS: User registration works over HTTP.");

const login = await request("/auth/login", {
  method: "POST",
  body: { email, password }
});
assert(login.response.status === 200 && login.payload.access_token, "Login did not return a session token.");
const token = login.payload.access_token;
console.log("PASS: Login returns a bearer session.");

const me = await request("/auth/me", { token });
assert(me.response.status === 200 && me.payload.email === email, "Authenticated identity check failed.");
console.log("PASS: Bearer token authenticates the current user.");

const strategyLibrary = await request("/strategies", { token });
assert(strategyLibrary.response.status === 200, "Strategy knowledge library could not be loaded.");
assert(strategyLibrary.payload.count >= 10, "Built-in reference strategies were not seeded.");
assert(strategyLibrary.payload.strategies.every(item => item.evidence_status === "UNVALIDATED"), "Reference techniques must not be treated as validated strategies.");
console.log("PASS: Authenticated user can read the unvalidated strategy reference library.");

const strategyContribution = await request("/strategies", {
  method: "POST",
  token,
  body: {
    name: "Integration test pullback",
    category: "TESTING",
    description: "A test-only strategy note for the integration smoke test.",
    entry_logic: "Wait for a defined trend and a confirmed pullback before considering an entry.",
    exit_logic: "Exit at the predefined target or when the trade thesis is invalidated.",
    risk_notes: "This is unvalidated test data and must remain within the one percent risk ceiling."
  }
});
assert(strategyContribution.response.status === 200, "Strategy contribution failed: " + JSON.stringify(strategyContribution.payload));
assert(strategyContribution.payload.evidence_status === "UNVALIDATED", "New strategy must default to unvalidated.");
console.log("PASS: Strategy knowledge can be contributed and remains unvalidated.");

const accountId = "INT-" + unique;
const created = await request("/accounts", {
  method: "POST",
  token,
  body: { account_id: accountId, starting_balance: 10000 }
});
assert(created.response.status === 200, "Paper account creation failed: " + JSON.stringify(created.payload));
console.log("PASS: Authenticated user can create a paper account.");

const accounts = await request("/accounts", { token });
assert(accounts.response.status === 200 && accounts.payload.count === 1, "Account list did not return the user's account.");
assert(accounts.payload.accounts[0].account_id === accountId, "Account ownership/list response mismatch.");
console.log("PASS: Account list returns the signed-in user's account.");

const account = await request("/accounts/" + encodeURIComponent(accountId), { token });
assert(account.response.status === 200 && account.payload.account_id === accountId, "Account detail retrieval failed.");
console.log("PASS: Account overview endpoint works.");

const risk = await request("/accounts/" + encodeURIComponent(accountId) + "/risk", { token });
assert(risk.response.status === 200 && risk.payload.maximum_risk_percent === 1, "Risk endpoint did not report the enforced 1% cap.");
console.log("PASS: Account risk endpoint reports the 1% maximum.");

const firstOrderId = "INT-ORDER-1-" + unique;
const secondOrderId = "INT-ORDER-2-" + unique;
let firstTrade;
let secondTrade;

const sequentialOrderChecks = [
  ["01 valid risk preview", async () => {
    const preview = await request("/risk/preview", {
      method: "POST",
      body: { account_balance: 10000, entry_price: 2000, stop_loss: 1990, risk_percent: 0.5, side: "BUY" }
    });
    assert(preview.response.status === 200 && preview.payload.approved === true, "Valid risk preview failed.");
    assert(Math.abs(preview.payload.risk_amount - 50) < 1e-8, "Risk preview did not calculate the expected R50 reservation.");
  }],
  ["02 open first paper order", async () => {
    const opened = await request("/trades", {
      method: "POST",
      token,
      body: { account_id: accountId, client_order_id: firstOrderId, symbol: "XAUUSD", side: "BUY", entry_price: 2000, stop_loss: 1990, risk_percent: 0.5 }
    });
    assert(opened.response.status === 200 && opened.payload.trade_id, "First paper order failed: " + JSON.stringify(opened.payload));
    firstTrade = opened.payload;
  }],
  ["03 reject duplicate order ID", async () => {
    const duplicate = await request("/trades", {
      method: "POST",
      token,
      body: { account_id: accountId, client_order_id: firstOrderId, symbol: "XAUUSD", side: "BUY", entry_price: 2000, stop_loss: 1990, risk_percent: 0.5 }
    });
    assert(duplicate.response.status === 400, "Duplicate client order ID was not rejected.");
  }],
  ["04 reject invalid BUY stop loss", async () => {
    const invalid = await request("/trades", {
      method: "POST",
      token,
      body: { account_id: accountId, client_order_id: "INT-BAD-" + unique, symbol: "XAUUSD", side: "BUY", entry_price: 2000, stop_loss: 2005, risk_percent: 0.5 }
    });
    assert(invalid.response.status === 400, "Invalid BUY stop loss was not rejected.");
  }],
  ["05 reserve first position risk", async () => {
    const current = await request("/accounts/" + encodeURIComponent(accountId) + "/risk", { token });
    assert(current.response.status === 200 && Math.abs(current.payload.current_open_risk - 50) < 1e-8, "First position risk reservation was not recorded.");
  }],
  ["06 open second paper order within remaining risk", async () => {
    const opened = await request("/trades", {
      method: "POST",
      token,
      body: { account_id: accountId, client_order_id: secondOrderId, symbol: "XAUUSD", side: "BUY", entry_price: 2000, stop_loss: 1990, risk_percent: 0.5 }
    });
    assert(opened.response.status === 200 && opened.payload.trade_id, "Second position within the 1% cap was rejected.");
    secondTrade = opened.payload;
  }],
  ["07 reject order beyond combined 1% cap", async () => {
    const excess = await request("/trades", {
      method: "POST",
      token,
      body: { account_id: accountId, client_order_id: "INT-OVER-" + unique, symbol: "XAUUSD", side: "BUY", entry_price: 2000, stop_loss: 1990, risk_percent: 0.5 }
    });
    assert(excess.response.status === 400, "Order exceeding the combined 1% risk cap was not rejected.");
  }],
  ["08 close first paper position", async () => {
    const closed = await request("/trades/" + encodeURIComponent(firstTrade.trade_id) + "/close", {
      method: "POST",
      token,
      body: { exit_price: 2010 }
    });
    assert(closed.response.status === 200 && closed.payload.status === "CLOSED", "First paper position did not close.");
  }],
  ["09 reconcile balance and remaining open risk", async () => {
    const [overview, current] = await Promise.all([
      request("/accounts/" + encodeURIComponent(accountId), { token }),
      request("/accounts/" + encodeURIComponent(accountId) + "/risk", { token })
    ]);
    assert(overview.response.status === 200 && Math.abs(overview.payload.current_balance - 10050) < 1e-8, "Realized P&L was not reconciled into account balance.");
    assert(current.response.status === 200 && Math.abs(current.payload.current_open_risk - 50) < 1e-8, "Closing first position did not release its risk reservation.");
  }],
  ["10 close final position and verify ledger", async () => {
    const closed = await request("/trades/" + encodeURIComponent(secondTrade.trade_id) + "/close", {
      method: "POST",
      token,
      body: { exit_price: 2010 }
    });
    assert(closed.response.status === 200 && closed.payload.status === "CLOSED", "Second paper position did not close.");
    const [overview, current, history] = await Promise.all([
      request("/accounts/" + encodeURIComponent(accountId), { token }),
      request("/accounts/" + encodeURIComponent(accountId) + "/risk", { token }),
      request("/accounts/" + encodeURIComponent(accountId) + "/trades", { token })
    ]);
    assert(overview.response.status === 200 && Math.abs(overview.payload.current_balance - 10100) < 1e-8, "Final balance does not reconcile with both realized closes.");
    assert(current.response.status === 200 && Math.abs(current.payload.current_open_risk) < 1e-8, "Open risk was not fully released.");
    assert(history.response.status === 200 && history.payload.trades.filter(item => item.status === "CLOSED").length === 2, "Closed trade ledger does not contain both positions.");
  }]
];

for (const [name, check] of sequentialOrderChecks) {
  await check();
  console.log("PASS: Sequential order check " + name + ".");
}
console.log("TAIMOBOT PAPER ORDER CHECKS: 10/10 PASSED SEQUENTIALLY.");

const trades = await request("/accounts/" + encodeURIComponent(accountId) + "/trades", { token });
assert(trades.response.status === 200 && Array.isArray(trades.payload.trades), "Trade history endpoint failed.");
console.log("PASS: Trade history endpoint returns a list.");

const logout = await request("/auth/logout", { method: "POST", token });
assert(logout.response.status === 200, "Logout failed.");
const afterLogout = await request("/auth/me", { token });
assert(afterLogout.response.status === 401, "Revoked session remained usable.");
console.log("PASS: Logout revokes the session.");

console.log("TAIMOBOT HTTP INTEGRATION: ALL CHECKS PASSED");
