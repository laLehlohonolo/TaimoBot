# Cost-aware paper-trade close accounting

## Status

Design and implementation review for the paper-trading branch. This document records the current implementation and the remaining gaps. It does not authorize live trading or broker execution.

## Implemented

- `backend/app/close_accounting.py` validates gross P&L and finite, non-negative spread, commission, swap, and other-cost amounts, then calculates net realized P&L.
- The trade ledger stores gross realized P&L, each supplied cost component, and net realized P&L in `realized_pnl`.
- Legacy closed trades are migrated without changing historical balances or historical P&L; their prior realized P&L is retained as gross/net with zero recorded costs. The migration is idempotent.
- A single requested trade close applies the caller-supplied costs to that requested trade.
- Group close allocates caller-supplied group costs across source positions by quantity, with the final position receiving the arithmetic remainder. Costs are not duplicated onto follower copies.
- Close operations validate resulting account balances and persist the affected trade/account changes transactionally.
- Existing tests cover cost arithmetic, migration, single-position preview/close parity, group preview/close parity, group allocation, and transactional rollback.

## Confirmed remaining gaps

### 1. Follower copy costs are not independently modelled

When a source trade is closed together with its open copies, supplied costs apply to the source trade only. Each follower copy is closed with zero recorded costs. Group close behaves similarly: group costs are allocated across source positions, while follower copies receive zero costs.

This avoids the unsafe alternative of blindly copying the source's absolute costs into separate follower accounts. However, follower realized P&L and balances can be overstated whenever follower-side charges should apply.

### 2. Previews do not cover the full close scope

- A single-trade preview evaluates only the requested trade, while closing an original source trade also closes its open direct copies.
- A group preview evaluates only source-group positions. It returns `preview_scope: SOURCE_GROUP_POSITIONS_ONLY` and `copy_positions_excluded` so the omission is visible.
- Preview and close currently use separate calculation paths (`profit_taking.py` and `close_accounting.py`) that have parity tests for source-only examples, but they do not share one cost-allocation engine.

A preview must not be presented as the expected total financial impact of a source-and-copy close while follower positions are excluded.

### 3. Cost inputs are caller-supplied and default to zero

Close and preview API fields default to zero for backward compatibility. There is no configured, versioned, per-account/per-instrument paper execution cost policy that derives those amounts. A zero value therefore means "zero supplied", not proof that an execution would incur no cost.

The paper engine's XAUUSD quantity remains abstract paper units, not broker lots. No verified broker contract sizing, tick-value conversion, account-currency conversion, spread, commission, swap, or slippage model is enabled.

## Required invariants

1. A closed trade's persisted realized P&L is net P&L: gross price movement minus the explicitly recorded cost components.
2. Each account balance reconciles to its starting balance plus its persisted realized net P&L, subject to the system's chosen currency precision.
3. A close operation is atomic across every trade and account it promises to close. Any validation, balance, or persistence failure leaves all affected trades and balances unchanged.
4. Existing callers that omit costs retain zero-cost compatibility, but APIs and UI must not describe omitted costs as verified zero-cost execution.
5. Costs are finite, non-negative amounts in the affected account's currency. Reject booleans, NaN, infinity, and overflow.
6. Preview and close use the same calculation and allocation engine, including the same close scope.
7. Store gross P&L and each cost component separately as well as net realized P&L.
8. Never apply a source position's absolute costs blindly to copied trades held in other accounts. Each account/position needs its own cost basis or an explicitly documented allocation rule.
9. Group-level costs are allocated deterministically across legs, with allocations summing exactly to the submitted group cost. Per-leg costs and group totals must not double-count.
10. No fixed dollar profit target is introduced. Adaptive take-profit evaluates net P&L and can recognize small positive net results.

## Proposed paper cost-policy contract

Before changing follower accounting, define a deterministic simulator policy. Do not invent numerical rates in application code.

Each policy record should include:

- immutable policy ID/version and effective-from timestamp;
- account or account class, instrument, and account currency;
- quantity/contract-unit definition and any required price/quote conversion;
- spread model and whether spread is represented in executable bid/ask prices or as an explicit monetary charge (never both);
- commission basis (per unit/lot, per side or round turn);
- swap basis, direction, holding-time/day-rollover rules, and applicable calendar;
- slippage model, kept separate from spread;
- other charges with explicit units and provenance;
- calculation timestamp and whether each amount is simulated/estimated or sourced from an actual execution report.

For every source and follower position, calculate costs independently using that account's applicable policy and the position's own quantity, entry/exit details, side, and holding period. Missing policy or conversion data must produce an explicit unavailable/estimated state; it must not silently become a verified zero.

The preview and close must call the same pure calculator and allocator. Preview must disclose every included and excluded position/account. The close transaction must persist each position's cost breakdown and policy version and reconcile each affected account. When an MT4/MT5 demo adapter is later introduced, broker-reported execution charges should be stored distinctly from simulator estimates.

## Implementation sequence

1. Specify and review the paper cost-policy contract and units; choose no rates until their provenance and assumptions are documented.
2. Add policy storage/versioning and a pure per-position cost calculator with deterministic rounding and a final-leg remainder for group allocation.
3. Make single-trade and group previews enumerate the exact source-and-copy close scope and calculate each account independently.
4. Integrate the same calculator into transactional single/group closes; persist policy provenance, estimated-versus-actual status, and per-position cost components.
5. Add tests for different source/follower policies, unequal quantities, holding periods, long/short positions, missing policies, stale policy versions, conversion failures, preview/close parity across copies and groups, account-balance reconciliation, duplicate/concurrent closes, and rollback.
6. Keep all new execution behaviour paper-only. Broker integration remains a separate demo-only gate requiring verified contract specifications and quote/cost provenance.

## Out of scope

- No live orders, real-money wallet posting, or production deployment.
- No enabling additional instruments. XAUUSD remains the only execution-approved symbol in this branch; other catalogue instruments remain observation-only.
- No change to the hard maximum 1% risk boundary.
