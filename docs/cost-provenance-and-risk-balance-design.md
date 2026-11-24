# Cost provenance and risk-balance safety design

**Status:** Design decision for review; not implemented.
**Scope:** Paper-trading close accounting, copied positions, account history, and order-risk eligibility.
**Safety invariant:** No account may open a new position using a balance that includes unresolved or unverified close results as though those results were final net equity. The hard 1% combined-risk ceiling remains unchanged.

## 1. Problem confirmed in the current implementation

- Close-cost request fields default to zero. The service therefore cannot distinguish omitted costs from a deliberately submitted zero unless request-field presence is preserved.
- Source closes apply supplied costs to the source position, while linked follower copies are closed with zero costs.
- The account balance is updated from the resulting `realized_pnl`.
- New position sizing and authenticated risk preview use `paper_accounts.balance` as the risk base.
- Account history and the dashboard present the resulting balance and realized P&L without cost-completeness metadata.
- Legacy closed trades are backfilled with zero cost columns, although the historical cost evidence is unavailable.

This means a provisional result can be presented as complete and can affect later position sizing.

## 2. Cost provenance states

Every closed position must carry an explicit cost status:

| Status | Meaning | May increase risk equity? |
|---|---|---|
| `UNKNOWN` | Costs omitted or unavailable; legacy rows also remain unknown | No |
| `MANUAL_UNVERIFIED` | Operator supplied values, but no independent evidence verifies them | No |
| `ESTIMATED` | Costs calculated by an explicitly approved, versioned paper-simulation policy | Yes, only under that policy |
| `ACTUAL` | Costs supported by a verified execution report | Yes |

A numeric zero is not evidence of zero cost. A caller-supplied zero is never `ACTUAL`. Do not label a manually supplied value as estimated unless it came from a named, versioned policy.

For the current paper-only system, there is no approved cost policy and no verified broker execution report. Therefore the system must not manufacture `ESTIMATED` or `ACTUAL` records.

## 3. Safe order-eligibility rule

Do not solve unknown costs by inventing a conservative fee amount or by silently subtracting an arbitrary reserve.

Instead, derive account risk eligibility from the persisted ledger:

- New orders and authenticated account risk previews are blocked while the account has any closed position whose cost status is `UNKNOWN` or `MANUAL_UNVERIFIED`.
- Existing open positions remain closable. A cost-completeness lock must not prevent risk-reducing closes.
- The ordinary emergency stop and TSSI security lock remain independent and higher-priority controls.
- Once all closed positions are supported by an approved versioned estimate or verified execution evidence, risk preview and new-order eligibility may resume.
- The block must be enforced inside the same write transaction that opens a position, not only in the frontend or a preflight endpoint.
- The generic unauthenticated calculator endpoint remains a mathematical calculator only; it must never authorize an account order.

This deliberately fails closed. It avoids allowing a provisional balance to enlarge the nominal 1% risk budget while no defensible cost estimate exists.

## 4. Ledger and balance treatment

- Preserve gross P&L, each cost component, net P&L, cost status, cost source, policy version (when applicable), and evidence reference (when applicable).
- Keep the account's displayed paper balance labelled provisional whenever unresolved close records exist.
- Do not silently rewrite historical rows as verified zero-cost trades.
- Preserve legacy P&L and balances during migration; mark legacy cost provenance `UNKNOWN`.
- Do not copy source absolute cost values to follower accounts. Each follower position needs its own account-currency cost calculation and provenance.
- Do not infer cross-currency costs or convert them without a fresh independently validated FX quote.
- Reconciliation must update the trade ledger and affected account balance atomically and idempotently.
- A cost-status change must be auditable and must identify the actor, reason, source/policy version, and timestamp.

## 5. API compatibility

- Preserve existing request field names during the first migration.
- Use Pydantic field-presence information (for example, `model_fields_set`) to distinguish omitted values from explicitly supplied values.
- Omitted costs become `UNKNOWN`; explicit caller-supplied costs become `MANUAL_UNVERIFIED`.
- Do not let request defaults convert omitted values into verified zero.
- Return explicit `cost_status`, `cost_complete`, `balance_status`, and `new_orders_allowed` fields from relevant account, history, close, and preview responses.
- Return a clear conflict response for risk preview/order attempts blocked by unresolved cost provenance.

## 6. Preview and close parity

A preview must state its exact scope. A source close can close direct follower copies; therefore a source-only preview must never be represented as the total impact.

Before enabling a complete preview:
- Enumerate the source position(s) and all open direct copies included in the close.
- Calculate each account's costs independently in that account's settlement currency.
- Use the same pure calculator and cost allocator for preview and close.
- Show any excluded position or account explicitly.
- Test single-position, group, source-plus-copy, and multi-follower cases for exact preview/close parity.

## 7. Required tests before implementation is accepted

1. Omitted costs remain `UNKNOWN`; explicit zero is `MANUAL_UNVERIFIED`.
2. Legacy rows remain `UNKNOWN` after repeated migrations; historical P&L and balances are unchanged.
3. A source's costs are never copied as absolute values to followers.
4. Each follower's cost policy is independently selected using its account and instrument.
5. Missing policy or unknown provenance blocks authenticated risk preview and new order creation.
6. The order-creation transaction rechecks eligibility to close preflight races.
7. Existing open positions can still be closed while new entries are blocked.
8. Approved versioned estimates permit risk eligibility only for the records covered by that policy.
9. A manual zero never qualifies as actual or estimated.
10. Account history, dashboard, trade history, and group history label provisional results.
11. Preview and close agree for every position/account in the declared scope.
12. Balance reconciliation, duplicate close, idempotence, concurrency, and rollback remain correct.
13. The 1% per-trade and combined-open-risk limits cannot be bypassed by provisional gains, stale previews, copied positions, or concurrent requests.

## 8. Rollout boundary

This is a design document only. No ledger, risk, balance, UI, or API behaviour has been changed by this document. Keep the PR draft, unmerged, and paper-only. Do not enable live trading or real-money handling.


## 9. Operating-mode separation required before implementation

The earlier blanket eligibility rule needs a product-level distinction so it does not silently turn ordinary paper accounts into unusable accounts.

### 9.1 Gross-only research simulation

- This mode is for strategy research, signal evaluation, and price-movement experiments.
- Results are explicitly labelled **GROSS / COSTS EXCLUDED / NOT NET PROFIT**.
- It must use an isolated research ledger or ephemeral simulation balance. It must not post gross-only results into a participant's account balance or account equity used for order sizing.
- It must not be represented as evidence that the account satisfies the 1% all-in risk requirement.
- It may continue to run when no approved cost policy exists because it does not authorize participant account orders or change their financial ledger.

### 9.2 Cost-aware participant paper accounts

- Participant paper-account orders use the account's persisted balance and the hard 1% combined open-risk limit.
- A new account-backed order is eligible only when every closed result affecting that account's balance has an approved cost provenance: a named, versioned simulation cost policy, or verified execution evidence where applicable.
- Missing or manually unverified costs must not be silently treated as zero, and must not increase the account's risk base.
- If the account has unresolved cost provenance, risk-increasing orders and account risk previews are blocked; existing positions remain closable.
- A policy must be explicitly approved, versioned, instrument/account-currency aware, and tested against preview/close parity before it can make an account eligible. No policy parameters are approved by this document.

### 9.3 No cross-mode balance contamination

- Research simulation balances, participant paper-account balances, and any future real-money wallet must remain separate ledgers.
- A gross-only research result must never be copied into a participant balance, used to raise a risk budget, or presented as a net result.
- A cost-policy version change applies prospectively unless an auditable reconciliation explicitly reprocesses eligible historical records. Never silently rewrite history.
- The existing 1% per-trade and combined-open-risk ceilings remain unchanged in account-backed trading.

### 9.4 Implementation gate

Before code changes, approve the cost-policy specification and its instrument/account-currency inputs. Then implement provenance, ledger migration, common close accounting, follower-specific cost calculation, complete preview scope, and UI labels. Until those gates pass, keep account-backed paper execution in its existing paper-only safety boundary and do not claim all-in risk compliance for balances with unresolved costs.
