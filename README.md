# django-entitlements

[![CI](https://github.com/NKSTUD/django-entitlements/actions/workflows/ci.yml/badge.svg)](https://github.com/NKSTUD/django-entitlements/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/django-entitlements.svg)](https://pypi.org/project/django-entitlements/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Plans, quotas and credits for Django. It answers the question every SaaS asks on every request: **can this customer do this, and how many times?**

```python
import entitlements

entitlements.subscribe(org, "pro")                      # from your Stripe webhook, a mobile money callback, an admin…
entitlements.can(org, "api")                            # True
with entitlements.use(org, "exports"):                  # consumed, and given back if the export fails
    export_pdf()
entitlements.grant(org, "exports", 50, key="pi_123")    # a pack they bought, used after the plan's allowance
entitlements.status(org, "exports")                     # Status(limit=500, used=37, credits=50, remaining=513, resets_at=…)
```

It doesn't take payments. Your billing provider charges the customer; your code tells django-entitlements what they bought; everything else asks django-entitlements what they're allowed to do. That split is what lets it work with Stripe, Paddle, Lemon Squeezy, mobile money, bank transfers and invoices at the same time.

## Why

Everyone rewrites this, and everyone hits the same bugs:

- **Quotas reset by cancelling and subscribing again.** Here usage is counted per billing period, not per subscription, so changing plan never resets it.
- **Credits lost on a plan change.** Credits are separate from plans and keep their own expiry.
- **Two requests spend the last credit twice.** Every balance moves with a conditional `UPDATE`, never read-then-write, and the test suite proves it on PostgreSQL with 30 threads at once.
- **A webhook delivered twice grants twice.** `subscribe()`, `grant()` and `consume()` all take an idempotency key.
- **A typo in a feature name gives free, unlimited access.** Unknown features raise an error.

The hosted services that solve this (Stigg, Schematic, Orb) are paid and sit on the request path. This is a Django app: your database, your admin, no network call.

## What it can model

| You want | You write |
|---|---|
| A switch | `"api": True` |
| A monthly allowance | `"exports": 500` with a feature that resets every `month` |
| Unlimited, still tracked | `"exports": None` |
| Overage billed later | `"exports": {"limit": 500, "overage": True}` |
| A limit on things that exist (projects, seats) | a `limit` feature and a counter function |
| Lifetime or daily, weekly, yearly allowances | `"period": "never"`, `"day"`, `"week"`, `"year"` |
| Resets on the billing date or on the 1st | `"anchor": "billing"` or `"calendar"` (in your `TIME_ZONE`) |
| Per-seat plans | `subscribe(org, "team", quantity=12)` multiplies the limits |
| Add-ons | an `addon` plan stacks on top of the base plan |
| Free tier | a `default` plan applies when nothing else does |
| Trials, fixed-term passes | `subscribe(user, "pro", ends_at=…)` |
| Credit packs that expire | `grant(user, "exports", 100, expires_at=…)` |
| A custom deal or a temporary unlock | `override(org, "exports", limit=10_000, ends_at=…)` |
| Plans for users, organizations or teams | any model instance is a subject |

## Install

```sh
pip install django-entitlements
```

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "django.contrib.contenttypes",
    "entitlements",
]
```

```sh
python manage.py migrate
```

Django 4.2 or later, Python 3.10 or later. Tested on SQLite and PostgreSQL.

## Define your catalog

Keep features and plans in code, so they're reviewed and versioned like everything else:

```python
# settings.py
ENTITLEMENTS = {
    "features": {
        "exports":    {"kind": "metered", "period": "month", "unit": "exports"},
        "ai_minutes": {"kind": "metered", "period": "month", "anchor": "calendar"},
        "api":        {"kind": "switch"},
        "projects":   {"kind": "limit"},
    },
    "plans": {
        "free":  {"default": True, "features": {"exports": 10, "projects": 1}},
        "pro":   {"features": {"exports": {"limit": 500, "overage": True}, "api": True, "projects": None,
                               "ai_minutes": 120}},
        "seats": {"addon": True, "features": {"projects": 5}},
    },
}
```

```sh
python manage.py sync_entitlements
```

Run it on every deploy; it's idempotent. A plan's features are replaced by the ones listed, and plans not listed are left alone. You can also call `entitlements.define(catalog)` from a data migration, or edit plans in the Django admin.

**Feature kinds**

- `switch`: on or off.
- `metered`: consumed with `consume()` or `use()`. Resets every `period` (`day`, `week`, `month`, `year`, or `never`), counted from the subscriber's billing anchor or from the calendar.
- `limit`: a cap on things that exist. Deleting a project frees a place. Tell it how to count:

```python
@entitlements.counter("projects")
def count_projects(org):
    return org.projects.count()
```

## Connect your billing

Call `subscribe()` when someone pays and `unsubscribe()` when they stop. With `external_id`, `subscribe()` updates the same subscription in place, so you can call it from every webhook without checking what you already stored.

**Stripe**

```python
def on_subscription_event(sub):  # customer.subscription.created / .updated
    org = Org.objects.get(stripe_customer=sub.customer)
    item = sub["items"].data[0]
    entitlements.subscribe(
        org,
        item.price.lookup_key,                      # "pro", "team"…
        quantity=item.quantity,
        external_id=sub.id,
        source="stripe",
        anchor=datetime.fromtimestamp(sub.billing_cycle_anchor, tz=UTC),
        ends_at=datetime.fromtimestamp(sub.cancel_at, tz=UTC) if sub.cancel_at else None,
    )

def on_subscription_deleted(sub):                   # customer.subscription.deleted
    entitlements.unsubscribe(external_id=sub.id)

def on_checkout_completed(session):                 # checkout.session.completed, for a one-off credit pack
    org = Org.objects.get(stripe_customer=session.customer)
    entitlements.grant(org, "exports", 100, key=session.id, source="stripe",
                       expires_at=timezone.now() + timedelta(days=365))
```

**A 30-day pass paid by mobile money or bank transfer**

```python
entitlements.subscribe(user, "pro", ends_at=timezone.now() + timedelta(days=30),
                       external_id=payment.reference, source="orange_money")
```

When `ends_at` passes, the subject falls back to the default plan. No scheduled job needed: periods, resets and expiries are worked out when you read.

**The billing anchor.** Monthly allowances reset on the subscriber's anchor (the first time they were seen, unless you pass `anchor=`). Pass your provider's billing cycle anchor so quotas reset the day the customer is charged. Changing plan without passing `anchor` keeps the current period's usage.

## Check and consume

```python
entitlements.can(org, "exports", 3)        # bool, reads only
entitlements.check(org, "api")             # raises Denied, consumes nothing
usage = entitlements.consume(org, "exports", 3, key=request_id)
entitlements.release(usage)                 # give it back, at most once
```

`consume()` takes from the plan's allowance first, then credits (those expiring first first), then records overage if the plan allows it. It's all or nothing: if the amount doesn't fit, nothing is taken and `Denied` is raised.

`use()` does both for you:

```python
with entitlements.use(org, "ai_minutes", minutes):
    transcribe(audio)                       # raises? the minutes are released
```

**Limits on things that exist.** Call `check()` inside the transaction that creates the thing. It locks the subscriber's row, so two requests can't both create the last allowed project:

```python
with transaction.atomic():
    entitlements.check(org, "projects")
    Project.objects.create(org=org, ...)
```

**Views.** `Denied` is a `PermissionDenied`, so Django and Django REST framework answer 403 if you let it through. The decorator consumes metered features and releases them if the view fails:

```python
@entitlements.requires("exports")
def export(request): ...

@entitlements.requires("api", subject=lambda request: request.user.org)
def api_view(request): ...
```

`Denied` carries `feature`, `reason` (`"disabled"` or `"limit"`) and `remaining`, so your frontend can offer the right upgrade.

## Show usage

```python
entitlements.status(org, "exports")
# Status(feature='exports', kind='metered', enabled=True, limit=500, used=37, credits=50, remaining=513,
#        overage=0, overage_allowed=True, period_start=…, resets_at=…)

entitlements.report(org)             # {"exports": Status(...), "api": Status(...), ...}
entitlements.subscriptions(org)      # active subscriptions, base plan first
```

`limit` and `remaining` are `None` when unlimited. Usage is recorded even then, and every consumption is kept in a ledger (`Usage`) with where it came from: plan, which credits, overage. Past periods stay in `Counter`, so overage can be billed and usage charted.

## Reference

| Function | |
|---|---|
| `define(catalog)` | Create or update features and plans. |
| `counter(feature)` | Decorator registering how to count a `limit` feature. |
| `subscribe(subject, plan, *, starts_at, ends_at, quantity, external_id, source, anchor, metadata)` | Give a plan. A base plan replaces the current one; an add-on stacks. |
| `unsubscribe(subject, plan=None, *, external_id=None, at=None)` | End subscriptions now or at a date. |
| `subscriptions(subject)` | Active subscriptions. |
| `grant(subject, feature, amount, *, expires_at, starts_at, key, source, note)` | Add credits. |
| `override(subject, feature, *, enabled, limit, ends_at, note)` | Replace what the plans give, for one feature. |
| `status(subject, feature)` / `report(subject)` | Where a subject stands. |
| `can(subject, feature, amount=1)` | Whether it's allowed now. |
| `check(subject, feature, amount=1)` | Raise `Denied` unless allowed. |
| `consume(subject, feature, amount=1, *, key)` | Use a metered feature. Returns a `Usage`. |
| `release(usage)` | Give a `Usage` back, once. |
| `use(subject, feature, amount=1, *, key)` | Context manager: consume, release on error. |
| `requires(feature, amount=1, *, subject)` | View decorator. |
| `forget(subject)` | Delete everything stored about a subject. |

## Development

```sh
uv sync
uv run python -m django test --settings=tests.settings tests                     # SQLite
DATABASE=postgres uv run python -m django test --settings=tests.settings tests   # PostgreSQL, with concurrency tests
uv run ruff check . && uv run ruff format --check .
```

Contributions are welcome. Please open an issue first for larger changes.

## License

MIT
