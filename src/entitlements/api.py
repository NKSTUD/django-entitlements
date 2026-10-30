"""Everything your code calls. `subject` is any model instance that holds plans: a user, an organization, a team."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, models, transaction
from django.db.models import F, Q, Sum
from django.db.models.functions import Greatest, Least
from django.utils import timezone

from . import periods
from .models import Counter, Entitlement, Feature, Grant, Override, Plan, Subscriber, Subscription, Usage

Kind = Feature.Kind

# Feature key -> function returning how many of that thing the subject has now. For `limit` features.
_counters: dict[str, Callable[[models.Model], int]] = {}


class Denied(PermissionDenied):
    """The subject can't do this. A PermissionDenied, so Django and DRF answer 403 if you let it through."""

    def __init__(self, feature: str, reason: str, remaining: int | None = None):
        self.feature, self.reason, self.remaining = feature, reason, remaining
        what = "is not included in the plan" if reason == "disabled" else "has reached its limit"
        super().__init__(f"'{feature}' {what}.")


@dataclass(frozen=True)
class Status:
    """Where a subject stands on one feature. `limit` and `remaining` are None when unlimited."""

    feature: str
    kind: str
    enabled: bool
    limit: int | None = None
    used: int = 0
    credits: int = 0
    remaining: int | None = None
    overage: int = 0
    overage_allowed: bool = False
    period_start: datetime | None = None
    resets_at: datetime | None = None

    def allows(self, amount: int = 1) -> bool:
        if not self.enabled:
            return False
        if self.kind == Kind.SWITCH or self.remaining is None or self.overage_allowed:
            return True
        return self.remaining >= amount


# --- Catalog ----------------------------------------------------------------------------------------------------


def define(catalog: dict) -> None:
    """Create or update features and plans from a dict, so your catalog lives in code and in version control.

    {"features": {"exports": {"kind": "metered", "period": "month"}, "api": {"kind": "switch"}},
     "plans": {"free": {"default": True, "features": {"exports": 10}},
               "pro": {"features": {"exports": {"limit": 500, "overage": True}, "api": True}}}}

    A plan's features are replaced by the ones listed. Plans and features not listed are left alone."""
    with transaction.atomic():
        features = {}
        for key, spec in catalog.get("features", {}).items():
            features[key], _ = Feature.objects.update_or_create(key=key, defaults=spec)
        for key, spec in catalog.get("plans", {}).items():
            spec = dict(spec)
            given = spec.pop("features", {})
            fields = {"is_default": spec.pop("default", False), "is_addon": spec.pop("addon", False), **spec}
            if fields["is_default"]:
                Plan.objects.filter(is_default=True).exclude(key=key).update(is_default=False)
            plan, _ = Plan.objects.update_or_create(key=key, defaults=fields)
            plan.entitlements.exclude(feature__key__in=given).delete()
            for fkey, value in given.items():
                feature = features.get(fkey) or Feature.objects.get(key=fkey)
                if isinstance(value, bool):
                    value = {"enabled": value}
                elif value is None or isinstance(value, int):
                    value = {"limit": value}
                Entitlement.objects.update_or_create(
                    plan=plan, feature=feature, defaults={"enabled": True, "limit": None, "overage": False, **value}
                )


def counter(feature: str) -> Callable:
    """Register how to count a `limit` feature:

    @entitlements.counter("projects")
    def count_projects(org):
        return org.projects.count()
    """

    def register(fn: Callable[[models.Model], int]) -> Callable[[models.Model], int]:
        _counters[feature] = fn
        return fn

    return register


# --- Subscriptions ----------------------------------------------------------------------------------------------


def subscribe(
    subject: models.Model,
    plan: str | Plan,
    *,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    quantity: int = 1,
    external_id: str | None = None,
    source: str = "",
    anchor: datetime | None = None,
    metadata: dict | None = None,
) -> Subscription:
    """Give a plan to a subject. A base plan ends the subject's other base plans; an add-on stacks.

    With `external_id` this is an upsert, so you can call it from every provider webhook: the subscription with that id
    is updated in place. `anchor` moves the billing cycle; leave it out and usage carries over across plan changes,
    so cancelling and subscribing again never resets a quota."""
    plan = plan if isinstance(plan, Plan) else Plan.objects.get(key=plan)
    starts_at = starts_at or timezone.now()
    with transaction.atomic():
        sub = _subscriber(subject, create=True, lock=True)
        if anchor and anchor != sub.anchor:
            sub.anchor = anchor
            sub.save(update_fields=["anchor"])
        fields = {"subscriber": sub, "plan": plan, "quantity": quantity, "ends_at": ends_at, "source": source}
        if metadata is not None:
            fields["metadata"] = metadata
        subscription = Subscription.objects.filter(external_id=external_id).first() if external_id else None
        if subscription:  # keeps its original starts_at
            for name, value in fields.items():
                setattr(subscription, name, value)
            subscription.save()
        else:
            subscription = Subscription.objects.create(**fields, starts_at=starts_at, external_id=external_id)
        if not plan.is_addon:  # a subject has one base plan at a time: the new one replaces the others
            (
                sub.subscriptions.filter(plan__is_addon=False, starts_at__lte=subscription.starts_at)
                .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=subscription.starts_at))
                .exclude(pk=subscription.pk)
                .update(ends_at=subscription.starts_at)
            )
        return subscription


def unsubscribe(
    subject: models.Model | None = None,
    plan: str | None = None,
    *,
    external_id: str | None = None,
    at: datetime | None = None,
) -> int:
    """End subscriptions now, or `at` a later date (end of the paid period). By subject (optionally one plan) or by
    provider id. Returns how many were ended. The subject falls back to the default plan."""
    at = at or timezone.now()
    if external_id:
        qs = Subscription.objects.filter(external_id=external_id)
    else:
        sub = _subscriber(subject)
        if sub is None:
            return 0
        qs = sub.subscriptions.all()
        if plan:
            qs = qs.filter(plan__key=plan)
    return qs.filter(Q(ends_at__isnull=True) | Q(ends_at__gt=at)).update(ends_at=at)


def subscriptions(subject: models.Model) -> list[Subscription]:
    """The subject's subscriptions active now, base plan first."""
    sub = _subscriber(subject)
    if sub is None:
        return []
    return sorted(_active(sub, timezone.now()), key=lambda s: s.plan.is_addon)


def grant(
    subject: models.Model,
    feature: str,
    amount: int,
    *,
    expires_at: datetime | None = None,
    starts_at: datetime | None = None,
    key: str | None = None,
    source: str = "",
    note: str = "",
) -> Grant:
    """Add credits on top of the plans, e.g. a pack someone bought. They are used after the plan's allowance, those
    expiring first first, and survive plan changes. With `key` (a payment id), calling twice grants once."""
    if amount < 1:
        raise ValueError("amount must be at least 1")
    feat = _feature(feature)
    if feat.kind != Kind.METERED:
        raise ValueError(f"'{feature}' is a {feat.kind} feature: credits only apply to metered features")
    sub = _subscriber(subject, create=True)
    fields = {
        "subscriber": sub,
        "feature": feat,
        "amount": amount,
        "remaining": amount,
        "starts_at": starts_at or timezone.now(),
        "expires_at": expires_at,
        "source": source,
        "note": note,
    }
    if key is None:
        return Grant.objects.create(**fields)
    try:
        with transaction.atomic():
            return Grant.objects.create(**fields, key=key)
    except IntegrityError:
        return Grant.objects.get(key=key)


def override(
    subject: models.Model,
    feature: str,
    *,
    enabled: bool = True,
    limit: int | None = None,
    ends_at: datetime | None = None,
    note: str = "",
) -> Override:
    """Replace what the plans give for one feature, until `ends_at` or forever. The latest override wins."""
    return Override.objects.create(
        subscriber=_subscriber(subject, create=True),
        feature=_feature(feature),
        enabled=enabled,
        limit=limit,
        ends_at=ends_at,
        note=note,
    )


def forget(subject: models.Model) -> None:
    """Delete everything stored about a subject. Call it when you delete the subject."""
    sub = _subscriber(subject)
    if sub:
        sub.delete()


# --- Checking and consuming -------------------------------------------------------------------------------------


def status(subject: models.Model, feature: str) -> Status:
    """Where the subject stands on one feature: included or not, limit, used, credits, remaining, next reset."""
    return _status(_subscriber(subject), _feature(feature), timezone.now(), subject)


def report(subject: models.Model) -> dict[str, Status]:
    """status() for every feature, keyed by feature key. Handy for a usage page."""
    sub, now = _subscriber(subject), timezone.now()
    return {f.key: _status(sub, f, now, subject) for f in Feature.objects.all()}


def can(subject: models.Model, feature: str, amount: int = 1) -> bool:
    """Whether the subject can use `amount` of the feature now. Reads only."""
    return status(subject, feature).allows(amount)


def check(subject: models.Model, feature: str, amount: int = 1) -> Status:
    """Raise Denied unless the subject can use `amount` now; consumes nothing.

    For `limit` features, call it inside transaction.atomic() with the creation it guards: it locks the subscriber
    row, so two requests can't both create the last allowed project."""
    feat = _feature(feature)
    in_transaction = not transaction.get_autocommit()
    sub = _subscriber(subject, create=in_transaction, lock=in_transaction)
    state = _status(sub, feat, timezone.now(), subject)
    if not state.enabled:
        raise Denied(feature, "disabled")
    if not state.allows(amount):
        raise Denied(feature, "limit", state.remaining)
    return state


def consume(subject: models.Model, feature: str, amount: int = 1, *, key: str | None = None) -> Usage:
    """Use `amount` of a metered feature: the plan's allowance first, then credits (expiring first), then overage if
    the plan allows it. All or nothing; raises Denied if it doesn't fit.

    Safe under concurrency on every database: each balance moves with a conditional UPDATE, never read-then-write.
    With `key` (a request or job id), retrying returns the first Usage instead of consuming twice."""
    if amount < 1:
        raise ValueError("amount must be at least 1")
    feat = _feature(feature)
    if feat.kind != Kind.METERED:
        raise ValueError(f"'{feature}' is a {feat.kind} feature: use check() instead")
    now = timezone.now()
    with transaction.atomic():
        sub = _subscriber(subject, create=True, now=now)
        if key is not None and (done := Usage.objects.filter(subscriber=sub, key=key).first()):
            return done
        ent = _resolve(sub, feat, now)
        if not ent.enabled:
            raise Denied(feature, "disabled")
        start, _ = _bounds(sub, feat, now)
        row, _ = Counter.objects.get_or_create(subscriber=sub, feature=feat, period_start=start)

        from_plan = _take_from_counter(row, amount, ent.limit)
        rest, from_grants = amount - from_plan, []
        if rest:
            rest, from_grants = _take_from_grants(sub, feat, rest, now)
        if rest:
            if not ent.overage:
                left = from_plan + sum(n for _, n in from_grants)
                raise Denied(feature, "limit", left)  # the atomic block puts back what was taken
            Counter.objects.filter(pk=row.pk).update(overage=F("overage") + rest)
        try:
            with transaction.atomic():
                return Usage.objects.create(
                    subscriber=sub,
                    feature=feat,
                    counter=row,
                    amount=amount,
                    from_plan=from_plan,
                    from_grants=from_grants,
                    overage=rest,
                    key=key,
                    created_at=now,
                )
        except IntegrityError:
            if key is None:
                raise
        # Same key consumed concurrently: undo ours, keep theirs.
        transaction.set_rollback(True)
    return Usage.objects.get(subscriber=sub, key=key)


def release(usage: Usage) -> bool:
    """Give back what a Usage took, e.g. the action failed. Goes back to the period and credits it came from.
    Returns False if it was already released, so calling it twice is harmless."""
    with transaction.atomic():
        if not Usage.objects.filter(pk=usage.pk, released_at__isnull=True).update(released_at=timezone.now()):
            return False
        Counter.objects.filter(pk=usage.counter_id).update(
            used=Greatest(F("used") - usage.from_plan, 0), overage=Greatest(F("overage") - usage.overage, 0)
        )
        for grant_id, n in usage.from_grants:
            Grant.objects.filter(pk=grant_id).update(remaining=Least(F("remaining") + n, F("amount")))
    usage.refresh_from_db(fields=["released_at"])
    return True


@contextlib.contextmanager
def use(subject: models.Model, feature: str, amount: int = 1, *, key: str | None = None) -> Iterator[Usage]:
    """Consume, run the block, and release if the block raises:  with entitlements.use(org, "exports"): export()"""
    usage = consume(subject, feature, amount, key=key)
    try:
        yield usage
    except BaseException:
        release(usage)
        raise


# --- Internals --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Resolved:
    enabled: bool
    limit: int | None
    overage: bool


def _feature(key: str) -> Feature:
    try:
        return Feature.objects.get(key=key)
    except Feature.DoesNotExist:
        raise LookupError(f"No feature '{key}'. Define it with entitlements.define() or in the admin.") from None


def _subscriber(subject, *, create: bool = False, lock: bool = False, now: datetime | None = None) -> Subscriber | None:
    if subject is None or subject.pk is None:
        raise ValueError("subject must be a saved model instance")
    ct = ContentType.objects.get_for_model(subject)
    qs = Subscriber.objects.filter(content_type=ct, object_id=str(subject.pk))
    if lock:
        qs = qs.select_for_update()
    if not create:
        return qs.first()
    sub = qs.first()
    if sub is None:
        try:
            with transaction.atomic():
                sub = Subscriber.objects.create(
                    content_type=ct, object_id=str(subject.pk), anchor=now or timezone.now()
                )
        except IntegrityError:  # created by a concurrent request
            sub = qs.get()
    return sub


def _active(sub: Subscriber, now: datetime) -> list[Subscription]:
    return list(
        sub.subscriptions.filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now), starts_at__lte=now).select_related(
            "plan"
        )
    )


def _resolve(sub: Subscriber | None, feat: Feature, now: datetime) -> _Resolved:
    """Combine everything that applies now: base plan (or the default one), add-ons times their quantity, override."""
    if sub is not None:
        ov = (
            Override.objects.filter(subscriber=sub, feature=feat, created_at__lte=now)
            .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
            .order_by("-created_at", "-pk")
            .first()
        )
    else:
        ov = None
    held = _active(sub, now) if sub is not None else []
    if not any(not s.plan.is_addon for s in held):
        default = Plan.objects.filter(is_default=True).first()
        if default:
            held.append(Subscription(plan=default, quantity=1))
    ents = {e.plan_id: e for e in Entitlement.objects.filter(feature=feat, plan__in=[s.plan for s in held])}
    enabled, limit, overage = False, 0, False
    for s in held:
        e = ents.get(s.plan.pk)
        if e is None or not e.enabled:
            continue
        enabled, overage = True, overage or e.overage
        limit = None if limit is None or e.limit is None else limit + e.limit * s.quantity
    if ov is not None:
        return _Resolved(ov.enabled, ov.limit, overage)
    return _Resolved(enabled, limit if enabled else 0, overage)


def _bounds(sub: Subscriber | None, feat: Feature, now: datetime) -> tuple[datetime, datetime | None]:
    if feat.period == Feature.Period.NEVER:
        return periods.bounds("never", now, now)
    if feat.anchor == Feature.Anchor.CALENDAR or sub is None:
        local = timezone.localtime(now)
        return periods.bounds(feat.period, local, periods.calendar_anchor(feat.period, local))
    return periods.bounds(feat.period, now, sub.anchor)


def _credits(sub: Subscriber, feat: Feature, now: datetime):
    return Grant.objects.filter(subscriber=sub, feature=feat, remaining__gt=0, starts_at__lte=now).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )


def _status(sub: Subscriber | None, feat: Feature, now: datetime, subject) -> Status:
    ent = _resolve(sub, feat, now)
    if feat.kind == Kind.SWITCH:
        return Status(feat.key, feat.kind, ent.enabled)
    if feat.kind == Kind.LIMIT:
        if feat.key not in _counters:
            raise LookupError(f"No counter for '{feat.key}'. Register one with @entitlements.counter('{feat.key}').")
        used = int(_counters[feat.key](subject))
        remaining = None if ent.limit is None else max(0, ent.limit - used)
        return Status(feat.key, feat.kind, ent.enabled, ent.limit, used, remaining=remaining if ent.enabled else 0)
    start, end = _bounds(sub, feat, now)
    row = Counter.objects.filter(subscriber=sub, feature=feat, period_start=start).first() if sub else None
    used, over = (row.used, row.overage) if row else (0, 0)
    credits = (_credits(sub, feat, now).aggregate(n=Sum("remaining"))["n"] or 0) if sub else 0
    remaining = None if ent.limit is None else max(0, ent.limit - used) + credits
    return Status(
        feat.key,
        feat.kind,
        ent.enabled,
        ent.limit,
        used,
        credits,
        remaining if ent.enabled else 0,
        over,
        ent.overage,
        start,
        end,
    )


def _take_from_counter(row: Counter, amount: int, limit: int | None) -> int:
    """Take up to `amount` from the period's allowance. Returns how much was taken."""
    if limit is None:
        Counter.objects.filter(pk=row.pk).update(used=F("used") + amount)
        return amount
    while True:
        row.refresh_from_db(fields=["used"])
        take = min(amount, max(0, limit - row.used))
        if not take:
            return 0
        # Only if nobody took the same room in the meantime; otherwise read again.
        if Counter.objects.filter(pk=row.pk, used__lte=limit - take).update(used=F("used") + take):
            return take


def _take_from_grants(sub: Subscriber, feat: Feature, amount: int, now: datetime) -> tuple[int, list[list[int]]]:
    """Take up to `amount` from credits, those expiring first first. Returns (what's left to find, [[id, n], …])."""
    taken: list[list[int]] = []
    order = (F("expires_at").asc(nulls_last=True), "created_at", "pk")
    while amount:
        g = _credits(sub, feat, now).exclude(pk__in=[i for i, _ in taken]).order_by(*order).first()
        if g is None:
            break
        n = min(amount, g.remaining)
        if Grant.objects.filter(pk=g.pk, remaining__gte=n).update(remaining=F("remaining") - n):
            taken.append([g.pk, n])
            amount -= n
        # else someone used it meanwhile: read it again on the next turn
    return amount, taken


def requires(feature: str, amount: int = 1, *, subject: Callable | None = None) -> Callable:
    """View decorator: answers 403 unless the subject can use the feature. Metered features are consumed, and released
    if the view raises or returns a 4xx/5xx response. `subject(request)` defaults to request.user."""
    import functools

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            holder = subject(request) if subject else request.user
            if _feature(feature).kind != Kind.METERED:
                check(holder, feature, amount)
                return view(request, *args, **kwargs)
            with use(holder, feature, amount) as usage:
                response = view(request, *args, **kwargs)
            if getattr(response, "status_code", 200) >= 400:
                release(usage)
            return response

        return wrapper

    return decorator
