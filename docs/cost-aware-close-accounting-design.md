# Cost-aware paper-trade close accounting

## Status

Design note only. No close-accounting behaviour or database schema has been changed by this document.

## Confirmed gap

Profit previews in `main.py` and `profit_taking.py` calculate gross price movement and subtract spread, commission, swap, and other costs. The persisted close paths currently calculate and store gross price-movement P&L only:

- Single source/follower close: `copy_trading.close_trade_with_copies`
- Group close including copies: `copy_trading.close_position_group_with_copies`
- Standalone execution close paths: `execution.close_paper_trade` and `execution.close_position_group`

Therefore preview net P&L can differ from realized P&L and the paper-account balance.

## Required invariants

1. A closed trade's persisted realized P&L is net P&L: gross price movement minus explicitly recorded costs.
2. The sum of realized P&L for an account reconciles exactly to its starting balance plus all realized net P&L (subject to the system's chosen currency precision).
3. A close operation is atomic across every trade and account it promises to close. Any validation, balance, or persistence failure leaves all affected trades and balances unchanged.
4. Existing callers that omit costs retain zero-cost behaviour.
5. Costs must be finite, non-negative, currency-denominated amounts; reject booleans, NaN, infinity, and overflow.
6. Preview and close must call the same calculation and allocation logic, not parallel implementations.
7. Store gross P&L and each cost component separately as well as net realized P&L, so history is auditable and costs are not lost in a single opaque number.
8. Do not apply a source position's absolute costs blindly to copied trades held in other accounts. Each account/position needs a defined cost basis or an explicitly documented allocation rule.
9. Group-level costs must be allocated deterministically across legs, with allocations summing exactly to the submitted group cost. Per-leg costs and group totals must not double-count.
10. No fixed dollar profit target is introduced. Adaptive take-profit continues to evaluate net P&L, including costs, and can recognize small positive net results.

## Implementation sequence

### 1. Data migration

Add nullable or zero-default cost ledger columns to `trades` for gross realized P&L, spread, commission, swap, other costs, and net realized P&L (or preserve `realized_pnl` as net and add the other components). Use an idempotent migration. For historical CLOSED trades, backfill gross/net from the existing `realized_pnl` and set costs to zero; do not reinterpret past results or change balances.

### 2. Shared calculation and allocation

Create one pure calculation function used by preview and close. Validate all numbers with the same finite-number validator. Define explicit per-trade and group-cost allocation contracts. Group allocation must use deterministic precision and a final-leg remainder so component totals equal the requested group total exactly.

### 3. API contracts

Extend close request models with optional cost components defaulting to zero. Group close accepts group-level cost components. For source-and-copy closure, require explicit cost inputs for each account/position or adopt a documented cost-rate allocation model; never silently duplicate the source's costs onto follower accounts. Keep the current API shape compatible when all cost fields are omitted.

### 4. Transactional close integration

Apply the shared net-P&L calculation inside the same database transaction that closes positions, updates copy execution states, and reconciles each account balance. Persist component costs and gross/net values together. Validate all resulting account balances before making any close writes. Preserve the existing all-or-nothing guarantee for source and copied positions.

### 5. Verification

Add tests for:
- zero-cost backward compatibility;
- each cost component and combined net P&L;
- long and short positions;
- small positive gross profit becoming a net loss after costs;
- small positive net profit still qualifying without a fixed dollar target;
- group allocation across unequal position quantities and rounding remainder;
- source/follower positions with distinct cost inputs;
- group close with copies across multiple accounts;
- negative resulting balance rejecting the entire close;
- duplicate/concurrent close attempts;
- database migration of legacy open and closed trades;
- account balance = starting balance + sum of persisted net realized P&L;
- preview and actual close producing identical net results for identical inputs.

## Current limitation

The paper engine's XAUUSD quantity remains abstract paper units, not broker lots. Real contract size, tick value, quote/account currency conversion, broker spread, commission, swap, and slippage require a separately validated MT4/MT5 demo adapter before any broker integration. No live trading is in scope.
