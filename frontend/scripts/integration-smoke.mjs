import { execFileSync } from "node:child_process";

const API = "http://127.0.0.1:4173/api";

function getSastClockParts() {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Africa/Johannesburg",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23"
  }).formatToParts(new Date());
  return Object.fromEntries(parts.filter(part => part.type !== "literal").map(part => [part.type, Number(part.value)]));
}

async function waitUntilMaintenanceEnds() {
  const now = getSastClockParts();
  if (now.hour !== 23) return;
  const waitMs = ((60 - now.minute) * 60 - now.second) * 1000 - (Date.now() % 1000) + 1000;
  console.log("MAINTENANCE: SAST 23:00-00:00 is active; waiting until 00:00 before paper-order integration checks.");
  await new Promise(resolve => setTimeout(resolve, waitMs));
  if (getSastClockParts().hour === 23) {
    throw new Error("Maintenance window did not end when expected; refusing to bypass the trading safeguard.");
  }
  console.log("PASS: Maintenance window ended; continuing paper-order integration checks.");
}

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
const email = "integration@taimobot.test";
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

// This smoke test owns an isolated, disposable SQLite database. Promote only
// its known CI fixture directly in that database; never expose an HTTP
// endpoint that can grant administrator privileges.
execFileSync("python", [
  "-c",
  "import sqlite3; db=sqlite3.connect('/tmp/taimobot-integration.db'); db.execute(\"UPDATE users SET role='ADMIN', email_verified=1 WHERE email=?\", ('integration@taimobot.test',)); db.commit(); db.close()"
]);

const testEntitlement = await request(
  "/auth/admin/participants/" + encodeURIComponent(me.payload.user_id) + "/subscription",
  {
    method: "PUT",
    token,
    body: {
      tier: "ADVANCED",
      reason: "Isolated CI integration fixture for source and follower paper accounts"
    }
  }
);
assert(
  testEntitlement.response.status === 200 && testEntitlement.payload.new_tier === "ADVANCED",
  "CI admin could not assign the Advanced test entitlement: " + JSON.stringify(testEntitlement.payload)
);
console.log("PASS: Isolated CI identity receives the explicitly authorised Advanced test entitlement.");

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
  body: { account_id: accountId, starting_balance: 10000, settlement_currency: "ZAR" }
});
assert(created.response.status === 200, "Paper account creation failed: " + JSON.stringify(created.payload));
console.log("PASS: Authenticated user can create a paper account.");

const accounts = await request("/accounts", { token });
assert(accounts.response.status === 200 && accounts.payload.count === 1, "Account list did not return the user's account.");
assert(accounts.payload.accounts[0].account_id === accountId, "Account ownership/list response mismatch.");
assert(accounts.payload.accounts[0].settlement_currency === "ZAR", "Account list did not return persisted settlement currency.");
console.log("PASS: Account list returns the signed-in user's account.");

const account = await request("/accounts/" + encodeURIComponent(accountId), { token });
assert(account.response.status === 200 && account.payload.account_id === accountId, "Account detail retrieval failed.");
assert(account.payload.settlement_currency === "ZAR", "Account detail did not return persisted settlement currency.");
console.log("PASS: Account overview endpoint works.");

const risk = await request("/accounts/" + encodeURIComponent(accountId) + "/risk", { token });
assert(risk.response.status === 200 && risk.payload.maximum_risk_percent === 1, "Risk endpoint did not report the enforced 1% cap.");
assert(risk.payload.settlement_currency === "ZAR", "Risk endpoint did not return persisted settlement currency.");
console.log("PASS: Account risk endpoint reports the 1% maximum.");

await waitUntilMaintenanceEnds();

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

const followerAccountId = "COPY-FOLLOWER-" + unique;
let copyRelationshipId;
let copiedSourceTrade;
let copiedFollowerTrade;

const sequentialCopyChecks = [
  ["01 create follower paper account", async () => {
    const createdFollower = await request("/accounts", {
      method: "POST",
      token,
      body: { account_id: followerAccountId, starting_balance: 5000, settlement_currency: "ZAR" }
    });
    assert(createdFollower.response.status === 200, "Follower paper account creation failed: " + JSON.stringify(createdFollower.payload));
  }],
  ["02 create source-to-follower relationship", async () => {
    const createdRelationship = await request("/accounts/" + encodeURIComponent(followerAccountId) + "/copy-relationships", {
      method: "POST",
      token,
      body: { source_account_id: accountId }
    });
    assert(createdRelationship.response.status === 200 && createdRelationship.payload.status === "ACTIVE", "Copy relationship creation failed.");
    copyRelationshipId = createdRelationship.payload.relationship_id;
  }],
  ["03 persist and list relationship", async () => {
    const listed = await request("/accounts/" + encodeURIComponent(followerAccountId) + "/copy-relationships", { token });
    assert(listed.response.status === 200 && listed.payload.count === 1, "Follower relationship was not persisted.");
    assert(listed.payload.relationships[0].relationship_id === copyRelationshipId, "Listed relationship ID did not match.");
  }],
  ["04 pause copy relationship", async () => {
    const paused = await request("/copy-relationships/" + encodeURIComponent(copyRelationshipId) + "/pause", { method: "POST", token });
    assert(paused.response.status === 200 && paused.payload.status === "PAUSED", "Copy relationship did not pause.");
  }],
  ["05 source trade while paused remains source-only", async () => {
    const sourceOnly = await request("/trades", {
      method: "POST",
      token,
      body: { account_id: accountId, client_order_id: "COPY-PAUSED-" + unique, symbol: "XAUUSD", side: "BUY", entry_price: 4000, stop_loss: 3990, risk_percent: 0.5 }
    });
    assert(sourceOnly.response.status === 200, "Source trade failed while follower was paused.");
    assert(sourceOnly.payload.copy_summary && sourceOnly.payload.copy_summary.eligible_followers === 0, "Paused follower was incorrectly eligible for a copy.");
  }],
  ["06 verify paused follower received no position", async () => {
    const history = await request("/accounts/" + encodeURIComponent(followerAccountId) + "/trades", { token });
    assert(history.response.status === 200 && history.payload.trades.length === 0, "Paused follower unexpectedly received a trade.");
  }],
  ["07 resume copy relationship", async () => {
    const resumed = await request("/copy-relationships/" + encodeURIComponent(copyRelationshipId) + "/resume", { method: "POST", token });
    assert(resumed.response.status === 200 && resumed.payload.status === "ACTIVE", "Copy relationship did not resume.");
  }],
  ["08 replicate a new source position", async () => {
    const source = await request("/trades", {
      method: "POST",
      token,
      body: { account_id: accountId, client_order_id: "COPY-ACTIVE-" + unique, symbol: "XAUUSD", side: "BUY", entry_price: 4000, stop_loss: 3990, risk_percent: 0.5 }
    });
    assert(source.response.status === 200 && source.payload.copy_summary && source.payload.copy_summary.successful_copies === 1, "Active follower did not receive exactly one copy.");
    copiedSourceTrade = source.payload;
  }],
  ["09 verify follower trade linkage and independent risk", async () => {
    const [history, followerRisk] = await Promise.all([
      request("/accounts/" + encodeURIComponent(followerAccountId) + "/trades", { token }),
      request("/accounts/" + encodeURIComponent(followerAccountId) + "/risk", { token })
    ]);
    assert(history.response.status === 200 && history.payload.trades.length === 1, "Follower trade ledger does not contain exactly one copied position.");
    copiedFollowerTrade = history.payload.trades[0];
    assert(copiedFollowerTrade.copied_from_trade_id === copiedSourceTrade.trade_id, "Follower trade is not linked to its source.");
    assert(copiedFollowerTrade.symbol === copiedSourceTrade.symbol && copiedFollowerTrade.side === copiedSourceTrade.side, "Copied instrument or direction differs from source.");
    assert(Math.abs(copiedFollowerTrade.entry_price - copiedSourceTrade.entry_price) < 1e-8 && Math.abs(copiedFollowerTrade.stop_loss - copiedSourceTrade.stop_loss) < 1e-8, "Copied entry or stop loss differs from source.");
    assert(followerRisk.response.status === 200 && followerRisk.payload.current_open_risk <= followerRisk.payload.maximum_risk_amount + 1e-8, "Follower exceeded its own aggregate risk allowance.");
  }],
  ["10 close source and reconcile copied lifecycle", async () => {
    const closed = await request("/trades/" + encodeURIComponent(copiedSourceTrade.trade_id) + "/close", {
      method: "POST",
      token,
      body: { exit_price: 4010 }
    });
    assert(closed.response.status === 200 && closed.payload.status === "CLOSED" && closed.payload.closed_copies === 1, "Source close did not close its follower copy.");
    const [sourceAccount, followerAccount, followerHistory, followerRisk] = await Promise.all([
      request("/accounts/" + encodeURIComponent(accountId), { token }),
      request("/accounts/" + encodeURIComponent(followerAccountId), { token }),
      request("/accounts/" + encodeURIComponent(followerAccountId) + "/trades", { token }),
      request("/accounts/" + encodeURIComponent(followerAccountId) + "/risk", { token })
    ]);
    assert(sourceAccount.response.status === 200 && followerAccount.response.status === 200, "Account balance reconciliation request failed.");
    assert(followerHistory.response.status === 200 && followerHistory.payload.trades.find(item => item.trade_id === copiedFollowerTrade.trade_id).status === "CLOSED", "Follower trade ledger did not record the copied close.");
    assert(followerRisk.response.status === 200 && Math.abs(followerRisk.payload.current_open_risk) < 1e-8, "Follower close did not release reserved risk.");
    assert(followerAccount.payload.current_balance > 5000, "Follower realized paper P&L was not applied to its own balance.");
    assert(sourceAccount.payload.current_balance > 10100, "Source account balance did not retain its earlier reconciled closes and new close.");
  }]
];

for (const [name, check] of sequentialCopyChecks) {
  await check();
  console.log("PASS: Sequential copy check " + name + ".");
}
console.log("TAIMOBOT COPY LIFECYCLE CHECKS: 10/10 PASSED SEQUENTIALLY.");


const trades = await request("/accounts/" + encodeURIComponent(accountId) + "/trades", { token });
assert(trades.response.status === 200 && Array.isArray(trades.payload.trades), "Trade history endpoint failed.");
console.log("PASS: Trade history endpoint returns a list.");

const logout = await request("/auth/logout", { method: "POST", token });
assert(logout.response.status === 200, "Logout failed.");
const afterLogout = await request("/auth/me", { token });
assert(afterLogout.response.status === 401, "Revoked session remained usable.");
console.log("PASS: Logout revokes the session.");

console.log("TAIMOBOT HTTP INTEGRATION: ALL CHECKS PASSED");
