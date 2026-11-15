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

const trades = await request("/accounts/" + encodeURIComponent(accountId) + "/trades", { token });
assert(trades.response.status === 200 && Array.isArray(trades.payload.trades), "Trade history endpoint failed.");
console.log("PASS: Trade history endpoint returns a list.");

const logout = await request("/auth/logout", { method: "POST", token });
assert(logout.response.status === 200, "Logout failed.");
const afterLogout = await request("/auth/me", { token });
assert(afterLogout.response.status === 401, "Revoked session remained usable.");
console.log("PASS: Logout revokes the session.");

console.log("TAIMOBOT HTTP INTEGRATION: ALL CHECKS PASSED");
