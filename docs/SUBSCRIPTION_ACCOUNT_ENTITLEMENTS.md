# Subscription entitlements and participant account limits

**Status:** Backend enforcement implemented for paper-account creation and admin-assigned entitlements; expanded lifecycle and isolation verification is in progress.
**Scope:** Paper/demo accounts only. This document does not authorise live trading, real-money wallet access, or payment processing.

## Confirmed subscription tiers

A participant has one permanent platform identity (`user_id`). The active subscription entitlement attached to that identity determines the maximum number of paper trading accounts the participant may own.

| Subscription tier | Maximum paper accounts per participant ID |
|---|---:|
| Standard | 1 |
| Advanced | 3 |
| Premium | 5 |
| Exclusive | 10 |

The tier name **Advanced** is confirmed. These are account-count entitlements, not starting-balance limits, profit targets, or trading-risk allowances.

## Required behaviour

1. Enforce account limits by authenticated `user_id`, never by email display text, device, browser, session, IP address, or client-supplied participant identifier.
2. Count every paper account owned by the participant, including accounts created on another device or through another valid session.
3. Enforce the entitlement inside the same database write transaction that creates an account. Concurrent account-creation requests must not exceed the limit.
4. The client must not be able to submit or override its subscription tier or account allowance.
5. A participant must be able to see their current tier, maximum account allowance, accounts currently used, and remaining allowance.
6. Reject account creation at the limit with a clear response that identifies the tier limit; do not silently delete, overwrite, or reset an existing account.
7. Upgrading a tier increases the allowance without deleting or altering existing accounts, balances, settlement currencies, positions, or history.
8. Downgrading must not delete accounts or financial history. If the participant already owns more accounts than the new tier permits, preserve all existing accounts and block additional account creation until the count is below the new limit. Any future account closure/deactivation policy must preserve immutable history.
9. Each account remains independently isolated: balance, immutable settlement currency, risk profile, open positions, realized/unrealized P&L, costs, and trade history.
10. Enforce account ownership and account-count limits in backend services and database transactions. Frontend-only restrictions are not security controls.

## Entitlement authority and payment boundary

- The backend is the source of truth for the participant's active tier.
- Participants must never be able to self-assign Advanced, Premium, or Exclusive by editing a request, browser state, local storage, or API payload.
- Until an approved subscription/payment integration exists, tier changes must be an explicitly authorised administrator action with an audit record. Do not simulate payment success or imply that a subscription fee has been collected.
- Future payment integration must update entitlements only after independently verified provider events, using idempotent processing and an auditable subscription lifecycle.
- Payment failure, expiry, cancellation, refund, chargeback, or provider outage must follow a reviewed entitlement policy. Never erase accounts or balances as an automatic consequence.
- The exact subscription prices, billing intervals, grace periods, renewal rules, and refund policy are not defined by this account-limit requirement and must not be invented.

## Security and correctness requirements

- Use the authenticated user's immutable `user_id` from the validated session.
- Use a serialised write transaction (for SQLite, acquire the write lock before counting and inserting) to prevent concurrent requests racing past the limit.
- Enforce valid tier values in the database or a single backend entitlement module.
- Existing users must receive an explicit, conservative migration value of **Standard (1 account)** unless a verified subscription record proves another tier. Do not infer a paid tier from balances, trading performance, account count, or frontend state.
- Legacy accounts with no verified owner must not be reassigned or counted against an unrelated participant.
- Add audit events for administrator tier changes, including actor, participant ID, previous tier, new tier, timestamp, and reason. Do not log passwords, access tokens, or payment secrets.
- Return account allowance data from an authenticated backend endpoint. Treat it as display information; the account-creation transaction remains authoritative.

## Required test coverage

1. Standard can create one account and is rejected on the second.
2. Advanced can create three accounts and is rejected on the fourth.
3. Premium can create five accounts and is rejected on the sixth.
4. Exclusive can create ten accounts and is rejected on the eleventh.
5. Two different participant IDs have independent allowances.
6. Multiple devices/sessions for one participant share the same allowance.
7. A client cannot raise its tier or account limit in the create-account payload.
8. Concurrent account-creation requests cannot exceed the tier limit.
9. An administrator can change a tier only through an authorised, audited path.
10. An unauthorised participant cannot change their own or another participant's tier.
11. Upgrade preserves existing accounts and all their ledger/history data.
12. Downgrade preserves existing accounts and blocks only additional creation when over limit.
13. A failed account insert rolls back without consuming allowance.
14. Legacy users migrate to Standard unless a verified entitlement is present.
15. API allowance counts match persisted account ownership after restart and across sessions.

## Acceptance rule

The feature is complete only when server-side tests prove that the same `user_id` has one shared account allowance across sessions/devices, all four tier boundaries are enforced transactionally, tier changes are authorised and audited, and account/ledger history remains isolated and intact.

## Explicit non-goals

- No real-money account or wallet count is defined here.
- No live broker execution or real-money trading is authorised.
- No subscription prices or profitability guarantees are implied.
- The hard backend maximum of 1% risk per trade and combined open risk remains unchanged.
