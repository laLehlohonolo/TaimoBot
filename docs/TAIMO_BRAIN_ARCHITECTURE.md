# Taimo Brain — Central Intelligence Architecture

## Product identity and mission

**Taimo Brain** is TaimoBot's dedicated, platform-wide AI intelligence system. It is the central research, observation, analysis, coordination, memory, and recommendation capability for the TaimoBot ecosystem.

Its mission is to help TaimoBot become more informed, disciplined, secure, reliable, competitive, and sustainable over time. It continuously gathers verified information, observes platform behaviour, studies decisions and outcomes, identifies opportunities and weaknesses, and gives the administrator clear, evidence-backed recommendations.

Taimo Brain is not merely a participant-facing chatbot, a trading strategy, or an unrestricted autonomous operator. It is an intelligence and oversight system working across all seven layers.

## 24/7 operating requirement

Taimo Brain must be available and operational around the clock, every day. Continuous operation means persistent monitoring and event-driven processing—not that every expensive research or model-training task runs continuously.

It must use:
- A continuously supervised service with health checks, heartbeats, restart recovery, and alerting.
- Event-driven intake for market observations, trade lifecycle events, rejected decisions, profit-taking, losses, system events, security signals, participant-support patterns, and administrator actions.
- Durable queues and checkpoints so events are not silently lost during outages or deployments.
- Prioritised workers with bounded concurrency, rate limits, timeouts, and back-pressure.
- Scheduled deeper research, reconciliation, evaluation, and learning. The established 23:00–00:00 Africa/Johannesburg cycle remains the daily intensive research and next-session preparation window, not the only time Brain is active.
- Clear degraded/offline states. If Brain is unavailable, core trading safety and deterministic platform functions continue under their existing rules; they must not depend on an AI response to enforce risk limits or protect positions.

A 24/7 service objective requires monitoring and operational recovery; it must not be represented as a guarantee of zero downtime.

## Platform-wide intelligence responsibilities

Taimo Brain receives appropriately scoped, auditable information from all seven layers:

1. **Market:** instrument/provider status, market data, calendar events, source freshness, data coverage, anomalies, and market conditions.
2. **Analysis:** multi-timeframe observations, features, regimes, patterns, uncertainty, and evidence quality.
3. **Strategy:** approved strategy performance, candidate research, signal quality, rejected opportunities, and strategy degradation.
4. **Execution:** paper-order lifecycle, fills/simulated fills, latency, slippage assumptions, duplicate prevention, and execution errors. Brain observes and analyses; it does not bypass execution authorization.
5. **Risk:** risk calculations, exposure, correlation, drawdown, limit breaches, and rejected unsafe actions. Risk remains deterministic and authoritative; Brain cannot change or override the 1% maximum risk per trade.
6. **Learning:** versioned experiments, baseline comparisons, walk-forward evaluation, paper/shadow results, lessons, and approved model versions.
7. **Legacy:** durable institutional memory, architecture decisions, incident history, approved releases, audit references, and rollback knowledge.

Brain must not assume that it can literally know everything. Every insight must state its source, timestamp, scope, confidence or uncertainty, and known data limitations. Missing or unverified information must be labelled as such.

## Learning from the complete decision cycle

Brain must study more than completed winning trades. It must record and evaluate:
- Information available at the time of a decision.
- Trades entered and their reasons, context, execution and outcomes.
- Trades rejected, including whether rejection was correct and whether a missed opportunity can be evaluated fairly.
- Profits taken, including exit timing and net results after applicable costs.
- Losses, drawdowns, errors and potentially preventable failure modes.
- No-trade periods, near misses, cancellations, and system/risk blocks.

Counterfactual analysis must use only information that would have been available at the original decision time; it must guard against look-ahead bias. Brain must never claim that all losses can be prevented or that profit is guaranteed.

## Opportunity scouting and innovation

Brain should proactively scout for:
- Changes in market behaviour, volatility, liquidity, and instrument characteristics.
- Relevant economic events and reliable information sources.
- Emerging research, methods, tools, security advisories, dependency risks, and engineering improvements.
- Strategy weaknesses, unexplored hypotheses, data-quality gaps, operational bottlenecks, and participant-experience improvements.
- New instruments or integrations worth investigating.

Scouting produces ranked research proposals, not automatic activation. Each proposal must explain the evidence, source quality, expected value, uncertainty, costs, risks, affected layers, test plan, and reasons to reject or defer it. New instruments remain observation-only until independently verified and explicitly approved. Current execution scope and paper-only restrictions remain unchanged.

## Administrator-only Taimo Brain office

The complete Brain feed and internal intelligence must be available only to explicitly provisioned administrators through server-side authorization and MFA. The administrator office should show:
- A live operational overview: Brain health, worker/queue status, last heartbeat, active tasks, failed tasks, and data freshness.
- Cross-layer status and dependencies for all seven layers.
- Research feed, source records, market/event intelligence, discoveries, hypotheses, and uncertainty.
- Full decision-cycle analysis: entries, rejections, profit-taking, losses, and paper performance.
- Strategy/model baseline-versus-candidate comparisons and experiment history.
- Security, privacy, integrity, suspicious-access, dependency, and availability alerts from actually integrated tools.
- Improvement proposals, affected code/components, test evidence, approval state, release history, and rollback controls.
- Daily, weekly, monthly, yearly, and custom reporting, with individual and aggregate views only where access is authorized.
- Searchable, durable institutional memory and audit history.

Participant accounts must never receive internal Brain reports, model upgrades, experimental strategies, source code, private strategy parameters, security investigations, administrator notes, or other participants' information.

## Participant information boundary

Participants receive only account-scoped and approved information needed to use the service:
- Their own account status, permitted features, paper balance and trade history.
- Concise, factual explanations of their own trade entries or rejections, without exposing exploitable strategy details.
- Their own clearly labelled virtual/paper results and relevant costs.
- Relevant economic-calendar notices (such as CPI, central-bank decisions, and employment releases), with verified event time, source and timezone; no claim that an event guarantees market direction.
- Maintenance notices, service status and approved educational financial-literacy content.

No participant-facing message may promise earnings, guarantee profits, or claim losses will never occur.

## Administrator-controlled improvement and release

Brain may investigate, propose, draft, test in isolated environments, and report improvements. It must never approve its own proposal, grant itself privileges, alter production code directly, change the 1% risk ceiling, change execution permissions, disable emergency controls, or move funds.

Each improvement proposal must include:
- Problem and evidence, sources and timestamps.
- Affected layers, services, files, data and users.
- Expected benefit, uncertainty, risks and compatibility impact.
- Reproducible tests, historical walk-forward results, sample size and paper/shadow comparison.
- Migration, monitoring, staged rollout and rollback plans.

The administrator reviews and chooses approve for staging, reject, or request revision. The interface may provide an **Approve and Update** action, but approval must trigger the defined test and staging pipeline—not silently rewrite production. Production release requires a separate explicit confirmation after CI, security, migration, readiness and paper/shadow gates pass. High-impact changes to Risk, Execution, identity, permissions, security, or financial ledgers require enhanced independent review. Every proposal, decision, test, release and rollback is recorded in an append-only audit trail.

## Reliability, security and governance

- Use least-privilege service identities and narrowly scoped data access between layers.
- Encrypt sensitive data in transit and at rest; minimise identity data exposed to Brain.
- Keep secrets out of prompts, logs, participant views and model context.
- Treat external content, market feeds, documents and tool outputs as untrusted data, never as instructions to override policy.
- Validate model outputs against schemas and deterministic controls before any downstream use.
- Log access and material actions; protect audit records from alteration.
- Apply timeouts, rate limits, circuit breakers, queue limits and resource budgets.
- Monitor prompt injection, data poisoning, anomalous tool use, model drift, dependency vulnerabilities and unusual access where suitable detection is actually integrated.
- Provide kill switches for Brain's optional research/automation workers without disabling the deterministic Risk layer, protective order management, or emergency controls.
- Preserve paper-only trading, the existing instrument execution restrictions, and the separation of demo and any future real-money ledgers.

## Delivery stages and truthful status

Build incrementally:
1. Define event contracts, service boundaries, access scopes, and audit records.
2. Implement Brain service health, heartbeat, durable event intake, queueing, and restart recovery.
3. Connect read-only telemetry from the seven layers.
4. Build the administrator-only intelligence office and source-backed reports.
5. Add decision-cycle learning and opportunity scouting.
6. Add reproducible candidate evaluation and the administrator-controlled staging/release workflow.
7. Test outages, duplicate events, stale data, permission boundaries, prompt injection, queue overload, rollback, and failure isolation.

This document is a product and engineering specification. It does not claim that Taimo Brain, its 24/7 workers, dashboards, integrations, or autonomous research functions have already been implemented. Each stage must be delivered, tested, and reported separately.

## Non-negotiable principle

**Taimo Brain may inform every layer, but it must never become more powerful than the safeguards that protect TaimoBot and its participants.** It recommends; deterministic controls enforce; the administrator authorizes governed improvements.


## Strategy Intelligence Lab

Taimo Brain includes a dedicated **Strategy Intelligence Lab** for independently inventing, critically challenging, testing, comparing, and recommending strategy hypotheses. It may explore conventional and unconventional statistical, mathematical, behavioural, market-structure, volatility, multi-timeframe and hybrid approaches. Every hypothesis must be falsifiable, explainable, reproducible and tested without look-ahead leakage.

The initial discovery challenge is seven calendar days after the required historical data, experiment records and evaluation harness are operational. It must end with an administrator-only report, including rejected and inconclusive ideas; seven days is not proof of profitability. The long-term planning aspiration is approximately two or three materially distinct candidates per year, never a forced quota. A candidate may be combined with others only through a separately tested composite strategy with conflict handling, correlation/exposure analysis, net-of-cost comparisons and independent approval.

The Lab must preserve experiment history and challenge every proposal adversarially: state its hypothesis, supporting and opposing evidence, failure conditions, uncertainty, baseline comparison and what would falsify it. It must never claim sentience, guaranteed profits, or loss-free trading. Strategy research remains paper/shadow only; XAUUSD remains the only currently execution-enabled instrument, other instruments remain observation-only, and the deterministic backend 1% maximum risk boundary remains unchanged.

All strategy research and candidate reports are administrator-only. Brain may propose and test candidates but cannot approve its own work or deploy directly. The full research protocol, seven-day plan, evaluation gates, composite-strategy requirements and truthful implementation status are specified in [TAIMO_STRATEGY_INTELLIGENCE_LAB.md](TAIMO_STRATEGY_INTELLIGENCE_LAB.md). Do not start the seven-day challenge clock until the data and reproducible test infrastructure are ready.
