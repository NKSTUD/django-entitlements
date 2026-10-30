# Changelog

## 0.1.0

First release.

- Features of three kinds: switches, metered allowances that reset each day, week, month or year (or never), and limits on things that exist.
- Plans with a default plan, add-ons, per-seat quantities, unlimited values and overage.
- Subscriptions from any billing provider, idempotent with `external_id`; plan changes keep the period's usage.
- Credits with start and expiry dates, used after the plan's allowance, those expiring first first.
- Per-subject overrides.
- `consume()`, `release()` and `use()`, safe under concurrency on every database, with an idempotency key and a ledger.
- `status()`, `report()`, the `requires` view decorator, the `sync_entitlements` command and the Django admin.
