# Taimo Brain — Strategy Intelligence Lab

## Mission

The Strategy Intelligence Lab is a dedicated research and invention capability within Taimo Brain. It must independently investigate, formulate, challenge, test, compare, and recommend trading-strategy hypotheses. It must not be limited to copying conventional indicators or known strategies. It may explore statistical, mathematical, behavioural, multi-timeframe, market-structure, volatility, event-aware, and hybrid approaches, provided every hypothesis is explainable, testable, and evaluated without data leakage.

The Lab's objective is not to manufacture frequent strategies or promise profit. Its objective is to discover robust, evidence-supported approaches and clearly identify when no strategy is ready.

## First seven-day strategy challenge

After the Lab's underlying data, experiment tracking, and evaluation harness are operational, Taimo Brain should run an initial seven-calendar-day research challenge. The seven days are a discovery and research sprint, not proof of profitability and not a requirement to force a passing result.

Suggested work plan:

1. **Day 1 — Evidence inventory:** verify available history, source provenance, data quality, instrument metadata, timeframes, trading costs, and known gaps. Identify what can and cannot be tested.
2. **Day 2 — Market behaviour:** investigate trend, range, volatility, momentum, reversals, breakouts, session behaviour where relevant, event effects where supported, and changes between market regimes.
3. **Day 3 — Hypothesis generation:** formulate multiple genuinely distinct strategy hypotheses, including unconventional approaches. State the market mechanism, entry/exit logic, invalidation, intended instruments/timeframes, and conditions where no trade should be taken.
4. **Day 4 — Adversarial critique:** attempt to disprove each hypothesis; search for overfitting, look-ahead leakage, data snooping, survivorship bias, fragile parameters, hidden costs, regime dependence, and simpler competing explanations.
5. **Day 5 — Historical evaluation:** run reproducible chronological backtests with realistic spread, commission, swap, slippage assumptions and execution constraints where data permits. Separate training, validation and untouched holdout periods.
6. **Day 6 — Robustness:** run walk-forward and out-of-sample tests, sensitivity analysis, parameter perturbation, regime segmentation, drawdown/stress analysis, and paper/shadow simulation where the harness is ready.
7. **Day 7 — Administrator report:** present the hypotheses, strongest candidates, failed/rejected ideas, test evidence, limitations, risk profile, required further tests, and explicit recommendation: continue testing, revise, reject, or nominate for staging review.

If trustworthy data or a functioning test harness is unavailable, Brain must report the blocker and an incomplete result. It must not invent test results or label a strategy profitable.

## Deep-thinking and self-challenge protocol

For every hypothesis and candidate, Brain must answer:

- What precise market behaviour or inefficiency is the hypothesis trying to explain?
- What evidence supports it, and what alternative explanations exist?
- What information would have been available at the decision time?
- What observation would falsify the hypothesis?
- In which instruments, timeframes, sessions or regimes might it work, and where might it fail?
- Does it survive realistic trading costs, execution delays and adverse conditions?
- Is performance concentrated in a small number of trades or one unusual period?
- Does it remain robust when parameters and assumptions are changed modestly?
- Does it improve on a simple baseline and the currently approved strategy?
- What is the strongest argument against adopting it?
- Is there enough independent evidence to proceed, or is the correct decision to wait?

Brain must distinguish measured facts, model inference, hypothesis, uncertainty and unknowns. It must not claim human consciousness or subjective experience. "Deep thinking" means documented critical reasoning, adversarial self-review, evidence traceability, and willingness to reject its own proposals.

## Candidate record and experiment journal

Every strategy candidate receives a stable ID and immutable version history. Its record must include:

- Name, version, authoring process and creation timestamp.
- Hypothesis and rationale; instruments, data sources and timeframes.
- Exact entry, exit, invalidation, sizing interface and no-trade conditions.
- Features and parameter definitions, with source-code/configuration revision.
- Data snapshot identifiers, time ranges, data quality, gaps and provenance.
- Training, validation, holdout and walk-forward windows.
- Number of trades, exposure, turnover, win/loss distribution, expectancy, net profit/loss, drawdown, risk-adjusted metrics and uncertainty intervals where appropriate.
- Spread, commission, swap, slippage and other cost assumptions.
- Regime-specific results, stress tests, sensitivity tests and failure cases.
- Baseline comparison, limitations, known conflicts and recommendation.
- Test logs, approvals, deployment state, monitoring thresholds and rollback reference.

Store losing, inconclusive and rejected experiments too. Do not overwrite the history of a candidate to make it appear successful.

## Strategy evaluation gates

A candidate must pass all applicable gates before it can be recommended for staging:

1. **Data integrity:** correct timestamps, no future data leakage, verified provenance, explicit gaps and no fabricated observations.
2. **Reproducibility:** same version, data and configuration produce reproducible results.
3. **Chronological validation:** training/validation/holdout and walk-forward testing respect time order; the holdout is not repeatedly tuned against.
4. **Cost realism:** results are net of relevant trading costs and conservative execution assumptions.
5. **Statistical robustness:** sufficient sample size and market-regime coverage; uncertainty and multiple-testing risk are disclosed.
6. **Risk assessment:** drawdown, tail loss, correlated exposure, consecutive losses, concentration and stress behaviour are evaluated.
7. **Baseline superiority:** compare with a simple baseline and the currently approved strategy using predeclared metrics; win rate alone is insufficient.
8. **Paper/shadow evaluation:** candidates operate without live orders and are monitored against expected behaviour before any release consideration.
9. **Safety compatibility:** no candidate can change the backend hard maximum of 1% risk per trade, bypass Risk/Execution, disable emergency stop, or enable an unapproved instrument.

Passing a backtest is not proof of future profitability. A candidate may be rejected or remain inconclusive even when its historical return is positive.

## Long-term invention programme

The long-term planning target is approximately **two or three materially distinct, well-researched strategy candidates per year**. This is an aspiration, not a quota or deadline. Brain must never lower evidence standards or invent artificial differences to meet a count. Some years may produce fewer candidates or none.

The Lab should continuously monitor approved strategy degradation and emerging research opportunities. It may revisit, refine, or retire strategies when evidence changes. Existing strategies remain versioned and monitored; new candidates do not automatically replace them.

## Composite strategy research

Brain may investigate combining compatible strategies into a composite system, but only after each component has been independently specified and evaluated.

The composite study must:

- Define how components interact, how signals are weighted, and how conflicts/no-trade conditions are resolved.
- Test whether components provide genuinely complementary information rather than duplicate correlated signals.
- Measure combined exposure, correlation, drawdown, turnover, costs, tail risk and operational complexity.
- Compare the composite against each component individually and a simple baseline on identical chronological windows.
- Test component failure, missing data, regime shifts, conflicting signals and degraded performance.
- Avoid double-counting capital or risk when multiple components signal at once.
- Preserve the same hard 1% maximum risk per trade and all account-wide exposure controls.

A composite strategy is a new candidate with its own ID, version, evidence and approval lifecycle. It does not inherit approval merely because its components were previously approved.

## Administrator review and release

All strategy research, candidate details, code diffs, internal reasoning records, test outputs and strategy performance reports belong in the administrator-only Taimo Brain office. Participants must not see experimental strategies, private parameters, source code, or internal research.

For each candidate the administrator can choose:

- **Request more research**
- **Reject**
- **Approve for isolated staging / paper evaluation**

A separate explicit production approval is required after staging evidence and all release gates pass. High-impact changes to Risk, Execution, account permissions, identity, security or financial ledgers require enhanced independent review. Brain cannot approve its own work or deploy directly to production.

All releases must be versioned, observable and reversible, with documented monitoring and rollback conditions. The current product boundary remains paper/demo only; no live trading or real-money execution is authorised by this specification.

## Current execution scope

- XAUUSD remains the only currently execution-enabled instrument, within existing paper-trading controls.
- Other FX instruments and Deriv synthetic indices remain observation-only until instrument-specific data, contract specifications, cost models, sizing conversions and risk controls are verified and explicitly approved.
- Synthetic indices must be analysed using their own market characteristics, not assumed Forex session/fundamental behaviour.
- The hard maximum risk remains 1% per trade, enforced by deterministic backend Risk/Execution controls.

## Administrator deliverable

The seven-day report and every later candidate report must be private to administrators and include:

1. Executive recommendation and confidence/uncertainty.
2. Strategy hypotheses explored and why some were rejected.
3. Exact strategy logic and intended operating conditions.
4. Data sources, coverage, timeframe and limitations.
5. Reproducible test methodology and code/data versions.
6. Net-of-cost results, drawdown, risk and regime breakdown.
7. Adversarial critique and failure scenarios.
8. Comparison with current approved baseline and other candidates.
9. Further paper-test plan and measurable acceptance criteria.
10. Explicit administrator decision requested.

## Implementation status

This document specifies the target capability. It does not claim that the Strategy Intelligence Lab, seven-day challenge scheduler, strategy backtesting harness, composite strategy engine, or candidate approval UI is implemented. Deliver these in tested stages, beginning with experiment records and a reproducible evaluation harness. Do not start the seven-day clock until the data and test infrastructure can support a truthful report.
