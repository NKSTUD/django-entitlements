from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q


class Feature(models.Model):
    """Something a plan can give: an on/off switch, a metered allowance, or a limit on things that exist."""

    class Kind(models.TextChoices):
        SWITCH = "switch", "Switch (on or off)"
        METERED = "metered", "Metered (consumed, can reset each period)"
        LIMIT = "limit", "Limit (counts things that exist, like projects or seats)"

    class Period(models.TextChoices):
        NEVER = "never", "Never resets"
        DAY = "day", "Every day"
        WEEK = "week", "Every week"
        MONTH = "month", "Every month"
        YEAR = "year", "Every year"

    class Anchor(models.TextChoices):
        BILLING = "billing", "On the subscriber's billing anchor"
        CALENDAR = "calendar", "On the calendar (1st of the month, Monday, Jan 1)"

    key = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=200, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.METERED)
    period = models.CharField(
        max_length=10, choices=Period.choices, default=Period.MONTH, help_text="Metered features only."
    )
    anchor = models.CharField(
        max_length=10, choices=Anchor.choices, default=Anchor.BILLING, help_text="Metered features only."
    )
    unit = models.CharField(max_length=50, blank=True, help_text="Shown to people, e.g. 'exports' or 'minutes'.")

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.name or self.key


class Plan(models.Model):
    key = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=200, blank=True)
    is_default = models.BooleanField(
        default=False, help_text="Applies to subscribers with no active base plan. At most one."
    )
    is_addon = models.BooleanField(default=False, help_text="Stacks on top of the base plan instead of replacing it.")
    metadata = models.JSONField(default=dict, blank=True, help_text="Yours: prices, provider ids, display order…")
    features = models.ManyToManyField(Feature, through="Entitlement", related_name="plans")

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_default"], condition=Q(is_default=True), name="entitlements_one_default"
            )
        ]

    def __str__(self):
        return self.name or self.key


class Entitlement(models.Model):
    """What one plan gives for one feature. A feature missing from a plan is not included in it."""

    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="entitlements")
    feature = models.ForeignKey(Feature, on_delete=models.CASCADE, related_name="entitlements")
    enabled = models.BooleanField(default=True)
    limit = models.PositiveBigIntegerField(null=True, blank=True, help_text="Empty means unlimited.")
    overage = models.BooleanField(
        default=False, help_text="Allow use past the limit. The excess is recorded so you can bill it."
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["plan", "feature"], name="entitlements_plan_feature")]

    def __str__(self):
        return f"{self.plan} / {self.feature}"


class Subscriber(models.Model):
    """Any model instance that holds plans: a user, an organization, a team, a workspace."""

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.CharField(max_length=64)
    subject = GenericForeignKey("content_type", "object_id")
    anchor = models.DateTimeField(help_text="Start of the billing cycle. Billing-anchored periods count from here.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["content_type", "object_id"], name="entitlements_one_subscriber")
        ]

    def __str__(self):
        return f"{self.content_type.model} {self.object_id}"


class Subscription(models.Model):
    """A plan held by a subscriber between two dates. Comes from Stripe, mobile money, an invoice or an admin."""

    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name="subscriptions")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    quantity = models.PositiveIntegerField(default=1, help_text="Multiplies the plan's limits, e.g. per seat.")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True, help_text="Empty means until it is ended.")
    external_id = models.CharField(
        max_length=255, null=True, blank=True, unique=True, help_text="Provider id, e.g. a Stripe subscription id."
    )
    source = models.CharField(max_length=50, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at"]
        indexes = [models.Index(fields=["subscriber", "starts_at"])]

    def __str__(self):
        return f"{self.subscriber}: {self.plan}"


class Override(models.Model):
    """Replaces what the plans give for one feature, e.g. a custom deal or a temporary unlock."""

    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name="overrides")
    feature = models.ForeignKey(Feature, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=True)
    limit = models.PositiveBigIntegerField(null=True, blank=True, help_text="Empty means unlimited.")
    ends_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.subscriber}: {self.feature}"


class Grant(models.Model):
    """Credits added on top of the plans: a pack someone bought, a gift, a refund. Used after the plan's allowance."""

    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name="grants")
    feature = models.ForeignKey(Feature, on_delete=models.CASCADE)
    amount = models.PositiveBigIntegerField()
    remaining = models.PositiveBigIntegerField()
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True, blank=True, help_text="Empty means it never expires.")
    key = models.CharField(
        max_length=255, null=True, blank=True, unique=True, help_text="Makes grant() idempotent, e.g. a payment id."
    )
    source = models.CharField(max_length=50, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["subscriber", "feature", "remaining"])]

    def __str__(self):
        return f"{self.subscriber}: {self.remaining}/{self.amount} {self.feature}"


class Counter(models.Model):
    """How much of a metered feature a subscriber used in one period. Old periods stay as history."""

    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name="counters")
    feature = models.ForeignKey(Feature, on_delete=models.CASCADE)
    period_start = models.DateTimeField()
    used = models.PositiveBigIntegerField(default=0, help_text="Taken from the plans' allowance.")
    overage = models.PositiveBigIntegerField(default=0, help_text="Used past the limit, where overage is allowed.")

    class Meta:
        ordering = ["-period_start"]
        constraints = [
            models.UniqueConstraint(fields=["subscriber", "feature", "period_start"], name="entitlements_one_counter")
        ]

    def __str__(self):
        return f"{self.subscriber}: {self.used} {self.feature} since {self.period_start:%Y-%m-%d}"


class Usage(models.Model):
    """One consumption, and where it was taken from, so it can be released exactly once."""

    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name="usages")
    feature = models.ForeignKey(Feature, on_delete=models.CASCADE)
    counter = models.ForeignKey(Counter, on_delete=models.CASCADE, related_name="usages")
    amount = models.PositiveBigIntegerField()
    from_plan = models.PositiveBigIntegerField(default=0)
    from_grants = models.JSONField(default=list, blank=True, help_text="[[grant id, amount], …]")
    overage = models.PositiveBigIntegerField(default=0)
    key = models.CharField(max_length=255, null=True, blank=True, help_text="Makes consume() idempotent.")  # noqa: DJ001
    created_at = models.DateTimeField()
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["subscriber", "key"], condition=Q(key__isnull=False), name="entitlements_usage_key"
            )
        ]

    def __str__(self):
        return f"{self.subscriber}: {self.amount} {self.feature}"
