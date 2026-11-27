# Market Layer Telemetry: Provider Contract and Readiness Gate

## Purpose

Define the first genuine provider-backed health check for the MARKET layer without converting data availability into a claim of instrument or execution readiness.

This document is an implementation contract. It does not claim that a live provider, scheduler, XAUUSD feed, or broker specification is configured.

## Current verified implementation boundary

The repository currently contains:

- A read-only Deriv public WebSocket client for active-symbol discovery and historical candles.
- Provider-response normalization for the supported Deriv synthetic-index catalogue.
- A persistent market-observation store with idempotent inserts.
- Raw coverage diagnostics and Deriv synthetic-series freshness/continuity diagnostics.
- Authenticated market observation and quality routes.
- An administrator-only seven-layer health inventory.

The Deriv client does not implement authorization, proposal, buy, sell, or order methods. Deriv synthetic indices are observation candidates only; their execution model is not configured.

## Non-negotiable instrument boundary

XAUUSD remains the initial TaimoBot instrument, but its market-data source, symbol mapping, feed contract, and broker specifications have not been verified in this implementation.

A fresh Deriv synthetic-index candle is not evidence of a healthy XAUUSD feed. It must never satisfy the XAUUSD Market-layer readiness gate.

Until a trusted XAUUSD provider and broker/instrument specification adapter are independently verified:

- XAUUSD signal generation and execution remain blocked.
- Other instruments remain observation-only unless separately verified.
- No provider health result can enable execution.
- The account-wide 1% open-risk ceiling and per-trade 1% maximum remain unchanged.
- Paper-only operation remains in force.

## Provider interface

A future MARKET provider must return a structured, timestamped observation containing:

- `provider_id` and immutable `provider_version`
- `instrument` and canonical symbol mapping
- `observed_at` and `checked_at`, both timezone-aware UTC timestamps
- `connectivity_status`
- `latest_observation_at`
- `age_seconds` and instrument/timeframe-specific `stale_after_seconds`
- `expected_interval_seconds`
- `missing_candle_count`, `invalid_candle_count`, and `future_candle_count`
- `coverage_window_start`, `coverage_window_end`, and a coverage measure only when its denominator is valid
- `source_reference` and a stable evidence identifier
- `provenance_status` and `broker_specification_status`
- An explicit `execution_eligible: false` field at the market-observation boundary

The provider must not self-assert authenticity. Provenance is a separately verified gate based on trusted source configuration, instrument mapping, and auditable evidence.

## Health-state mapping

The health provider reports evidence; the layer-health aggregator maps it to a status.

| Evidence | MARKET status |
|---|---|
| No provider registered | NOT_IMPLEMENTED |
| Provider cannot be reached or no observations exist | OFFLINE |
| XAUUSD provider exists but source, mapping, freshness, continuity, or provenance is not verified | BLOCKED |
| A verified XAUUSD source is reachable but has stale data, gaps, malformed candles, or incomplete coverage | DEGRADED |
| All required XAUUSD feed checks pass for the configured monitoring window | HEALTHY |

A healthy MARKET status means only that the configured market-data monitoring checks passed. It does not authorize a strategy, validate broker sizing specifications, or permit an order.

## Freshness and continuity rules

- Freshness thresholds must be configured per instrument and timeframe, not inferred from a generic default.
- Future-dated candles are invalid evidence and force a non-healthy state.
- Duplicate timestamps must be counted and investigated; they must not inflate coverage.
- Cadence alignment must be checked against the expected timeframe.
- Missing intervals must be calculated only from valid aligned timestamps.
- Coverage must not be reported as 100% for an empty or single-candle series.
- Session calendars, market closures, holidays, and maintenance windows must be explicit before session-adjusted coverage is claimed.
- Empty provider responses and epoch-zero placeholders never count as historical coverage.
- Provider timeouts, malformed responses, and schema changes must be visible as evidence, not silently converted to healthy states.

## Aggregation and security

- Provider observations are generated server-side and timestamped by the service.
- The administrator health endpoint remains protected by persisted ADMIN role and verified-email checks.
- Participants must not receive administrator health or provider internals.
- A missing or expired heartbeat cannot retain a prior HEALTHY status indefinitely.
- The aggregator preserves the last successful timestamp separately from the latest check timestamp.
- A provider cannot register itself as trusted or change its own instrument allowlist.
- Health reporting is read-only and must not call execution, signal-generation, or account mutation paths.

## Acceptance tests required before implementation is called complete

1. No provider registered returns NOT_IMPLEMENTED with null heartbeat timestamps.
2. Provider timeout or empty data returns OFFLINE, not HEALTHY.
3. A future-dated candle forces a non-healthy result.
4. Stale data returns DEGRADED or BLOCKED according to provenance readiness.
5. Missing intervals and invalid cadence are detected without inventing coverage.
6. Duplicate candles do not inflate unique coverage.
7. A single observation produces no percentage coverage.
8. A Deriv synthetic-index heartbeat cannot satisfy the XAUUSD health gate.
9. Unverified XAUUSD source or broker specifications keep the instrument BLOCKED.
10. Participant access to administrator health remains denied.
11. Health evaluation cannot create a signal, paper order, live order, or alter risk settings.

## Delivery sequence

1. Keep the seven-layer inventory honest while no provider is registered.
2. Implement a provider-neutral health observation schema and deterministic status mapper.
3. Add a read-only Deriv synthetic-series provider for diagnostics only, clearly scoped to its own symbols.
4. Add the XAUUSD provider only after its feed, symbol mapping, and source contract are verified.
5. Connect authenticated provider evidence to the admin health aggregator.
6. Run the full backend and frontend CI suites before claiming completion.

## Current status

This document records the acceptance contract and implementation sequence. It does not mark MARKET or any other layer healthy, and it does not claim a verified XAUUSD feed.

## Server-controlled provider configuration boundary

The backend now has a fail-closed environment-backed configuration registry in `backend/app/market_provider_registry.py`. It reads only process environment values; participant requests cannot set or override trust flags.

The optional configuration keys are:

- `TAIMOBOT_MARKET_PROVIDER_ID`
- `TAIMOBOT_MARKET_SOURCE_VERIFIED`
- `TAIMOBOT_MARKET_SYMBOL_MAPPING_VERIFIED`
- `TAIMOBOT_MARKET_PROVENANCE_VERIFIED`
- `TAIMOBOT_MARKET_BROKER_SPECIFICATION_VERIFIED`

Verification flags are true only when the corresponding server value is explicitly `true` (case-insensitive, surrounding whitespace ignored). Missing, malformed, or any other value is false. A provider ID by itself does not verify any source or broker property.

This configuration registry is not a live provider adapter: no heartbeat or market evidence is ingested by it. The admin health endpoint therefore remains `NOT_IMPLEMENTED` when no evidence is supplied, even if an operator has prepared environment configuration. Do not populate the verification flags until independent evidence and operator review support each claim. Never place provider secrets in these identifiers or expose environment values through API responses.

The registry tests cover absent configuration, provider identity without verification, explicit true values, malformed flags, and blank provider IDs.
