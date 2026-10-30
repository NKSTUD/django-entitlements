from django.contrib import admin

from .models import Counter, Entitlement, Feature, Grant, Override, Plan, Subscriber, Subscription, Usage


@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    list_display = ["key", "name", "kind", "period", "anchor", "unit"]
    list_filter = ["kind", "period"]
    search_fields = ["key", "name"]


class EntitlementInline(admin.TabularInline):
    model = Entitlement
    extra = 0
    autocomplete_fields = ["feature"]


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ["key", "name", "is_default", "is_addon"]
    list_filter = ["is_default", "is_addon"]
    search_fields = ["key", "name"]
    inlines = [EntitlementInline]


class SubscriptionInline(admin.TabularInline):
    model = Subscription
    extra = 0
    fields = ["plan", "quantity", "starts_at", "ends_at", "external_id", "source"]


class GrantInline(admin.TabularInline):
    model = Grant
    extra = 0
    fields = ["feature", "amount", "remaining", "starts_at", "expires_at", "source", "note"]


class OverrideInline(admin.TabularInline):
    model = Override
    extra = 0
    fields = ["feature", "enabled", "limit", "ends_at", "note"]


class CounterInline(admin.TabularInline):
    model = Counter
    extra = 0
    max_num = 0
    can_delete = False
    fields = readonly_fields = ["feature", "period_start", "used", "overage"]


@admin.register(Subscriber)
class SubscriberAdmin(admin.ModelAdmin):
    list_display = ["__str__", "subject", "anchor", "created_at"]
    list_filter = ["content_type"]
    search_fields = ["object_id"]
    readonly_fields = ["content_type", "object_id", "created_at"]
    inlines = [SubscriptionInline, GrantInline, OverrideInline, CounterInline]


@admin.register(Usage)
class UsageAdmin(admin.ModelAdmin):
    """The ledger. Read-only: balances move through entitlements.consume() and release()."""

    list_display = ["created_at", "subscriber", "feature", "amount", "from_plan", "overage", "released_at"]
    list_filter = ["feature"]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
