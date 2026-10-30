import threading
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import time_machine
from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

import entitlements
from entitlements import Denied
from entitlements.models import Counter, Grant, Plan, Subscription, Usage
from entitlements.periods import add_months, bounds, calendar_anchor
from tests.testapp.models import Org, Project

UTC = dt_timezone.utc

CATALOG = {
    "features": {
        "exports": {"kind": "metered", "period": "month", "unit": "exports"},
        "ai_minutes": {"kind": "metered", "period": "month", "anchor": "calendar"},
        "lifetime_scans": {"kind": "metered", "period": "never"},
        "api": {"kind": "switch"},
        "projects": {"kind": "limit"},
    },
    "plans": {
        "free": {"default": True, "features": {"exports": 3, "projects": 1, "lifetime_scans": 2}},
        "pro": {
            "features": {"exports": {"limit": 100, "overage": True}, "api": True, "projects": None, "ai_minutes": 60}
        },
        "team": {"features": {"exports": 50, "api": True, "projects": 10}},
        "seats": {"addon": True, "features": {"exports": 10, "projects": 2}},
    },
}


@entitlements.counter("projects")
def count_projects(org):
    return org.projects.count()


def at(*args):
    return datetime(*args, tzinfo=UTC)


class Base(TestCase):
    def setUp(self):
        entitlements.define(CATALOG)
        self.org = Org.objects.create(name="acme")


class PeriodTests(TestCase):
    def test_add_months_clamps_and_comes_back(self):
        jan31 = at(2026, 1, 31, 9)
        self.assertEqual(add_months(jan31, 1), at(2026, 2, 28, 9))
        self.assertEqual(add_months(jan31, 2), at(2026, 3, 31, 9))
        self.assertEqual(add_months(jan31, -2), at(2025, 11, 30, 9))
        self.assertEqual(add_months(at(2024, 2, 29), 12), at(2025, 2, 28))

    def test_monthly_bounds_from_anchor(self):
        anchor = at(2026, 1, 31, 9)
        self.assertEqual(bounds("month", at(2026, 3, 15), anchor), (at(2026, 2, 28, 9), at(2026, 3, 31, 9)))
        self.assertEqual(bounds("month", at(2026, 3, 31, 9), anchor), (at(2026, 3, 31, 9), at(2026, 4, 30, 9)))
        self.assertEqual(bounds("month", at(2026, 1, 31, 8), anchor), (at(2025, 12, 31, 9), at(2026, 1, 31, 9)))

    def test_other_periods(self):
        anchor = at(2026, 1, 1, 12)
        self.assertEqual(bounds("day", at(2026, 1, 3, 11), anchor), (at(2026, 1, 2, 12), at(2026, 1, 3, 12)))
        self.assertEqual(bounds("week", at(2026, 1, 20), anchor), (at(2026, 1, 15, 12), at(2026, 1, 22, 12)))
        self.assertEqual(bounds("year", at(2027, 1, 1, 11), anchor), (at(2026, 1, 1, 12), at(2027, 1, 1, 12)))
        self.assertEqual(bounds("never", at(2030, 1, 1), anchor)[1], None)

    def test_calendar_anchor(self):
        now = at(2026, 10, 30, 15, 45)  # a Friday
        self.assertEqual(calendar_anchor("month", now), at(2026, 10, 1))
        self.assertEqual(calendar_anchor("week", now), at(2026, 10, 26))
        self.assertEqual(calendar_anchor("year", now), at(2026, 1, 1))
        self.assertEqual(calendar_anchor("day", now), at(2026, 10, 30))


class CatalogTests(Base):
    def test_define_is_idempotent_and_replaces_a_plans_features(self):
        entitlements.define(CATALOG)
        self.assertEqual(Plan.objects.count(), 4)
        entitlements.define({"plans": {"team": {"features": {"exports": 75}}}})
        team = Plan.objects.get(key="team")
        self.assertEqual([(e.feature.key, e.limit) for e in team.entitlements.all()], [("exports", 75)])

    def test_one_default_plan(self):
        entitlements.define({"plans": {"team": {"default": True, "features": {"exports": 75}}}})
        self.assertEqual(list(Plan.objects.filter(is_default=True).values_list("key", flat=True)), ["team"])

    @override_settings(ENTITLEMENTS={"features": {"x": {"kind": "switch"}}, "plans": {"p": {"features": {"x": True}}}})
    def test_sync_command(self):
        call_command("sync_entitlements", stdout=open("/dev/null", "w"))  # noqa: SIM115
        self.assertTrue(Plan.objects.filter(key="p").exists())

    def test_unknown_feature_is_an_error_not_a_free_pass(self):
        with self.assertRaises(LookupError):
            entitlements.can(self.org, "exprots")


class DefaultPlanTests(Base):
    def test_no_subscription_means_default_plan_and_no_rows(self):
        s = entitlements.status(self.org, "exports")
        self.assertEqual((s.enabled, s.limit, s.used, s.remaining), (True, 3, 0, 3))
        self.assertFalse(entitlements.can(self.org, "api"))
        self.assertFalse(Counter.objects.exists())

    def test_feature_missing_from_plan_is_disabled(self):
        with self.assertRaises(Denied) as ctx:
            entitlements.consume(self.org, "ai_minutes")
        self.assertEqual(ctx.exception.reason, "disabled")

    def test_without_any_default_plan_nothing_is_included(self):
        Plan.objects.update(is_default=False)
        self.assertFalse(entitlements.can(self.org, "exports"))


class MeteredTests(Base):
    def test_consume_until_the_limit(self):
        entitlements.consume(self.org, "exports", 2)
        entitlements.consume(self.org, "exports")
        with self.assertRaises(Denied) as ctx:
            entitlements.consume(self.org, "exports")
        self.assertEqual((ctx.exception.reason, ctx.exception.remaining), ("limit", 0))
        self.assertEqual(entitlements.status(self.org, "exports").used, 3)

    def test_refused_consumption_takes_nothing(self):
        entitlements.grant(self.org, "exports", 1)
        with self.assertRaises(Denied) as ctx:
            entitlements.consume(self.org, "exports", 5)
        self.assertEqual(ctx.exception.remaining, 4)
        s = entitlements.status(self.org, "exports")
        self.assertEqual((s.used, s.credits, s.remaining), (0, 1, 4))

    def test_denied_is_a_permission_denied(self):
        from django.core.exceptions import PermissionDenied

        self.assertTrue(issubclass(Denied, PermissionDenied))

    def test_resets_each_billing_month_from_the_anchor(self):
        with time_machine.travel(at(2026, 1, 31, 10), tick=False):
            entitlements.subscribe(self.org, "team", anchor=at(2026, 1, 31, 10))
            entitlements.consume(self.org, "exports", 50)
            self.assertFalse(entitlements.can(self.org, "exports"))
        with time_machine.travel(at(2026, 2, 28, 9), tick=False):
            self.assertFalse(entitlements.can(self.org, "exports"))
        with time_machine.travel(at(2026, 2, 28, 10), tick=False):
            s = entitlements.status(self.org, "exports")
            self.assertEqual((s.used, s.period_start, s.resets_at), (0, at(2026, 2, 28, 10), at(2026, 3, 31, 10)))

    def test_calendar_period_follows_the_local_time_zone(self):
        entitlements.subscribe(self.org, "pro")
        # 23:30 UTC on Oct 31 is already Nov 1 in Casablanca (UTC+1): a new month there.
        with time_machine.travel(at(2026, 10, 31, 22, 30), tick=False):
            entitlements.consume(self.org, "ai_minutes", 60)
        with time_machine.travel(at(2026, 10, 31, 23, 30), tick=False):
            s = entitlements.status(self.org, "ai_minutes")
            self.assertEqual(s.used, 0)
            self.assertEqual(timezone.localtime(s.period_start).day, 1)

    def test_never_resets(self):
        entitlements.consume(self.org, "lifetime_scans", 2)
        with time_machine.travel(timezone.now() + timedelta(days=800), tick=False):
            self.assertFalse(entitlements.can(self.org, "lifetime_scans"))

    def test_unlimited_still_records_usage(self):
        entitlements.override(self.org, "exports", limit=None)
        entitlements.consume(self.org, "exports", 1000)
        s = entitlements.status(self.org, "exports")
        self.assertEqual((s.limit, s.remaining, s.used), (None, None, 1000))

    def test_overage_is_allowed_and_recorded(self):
        entitlements.subscribe(self.org, "pro")
        usage = entitlements.consume(self.org, "exports", 120)
        s = entitlements.status(self.org, "exports")
        self.assertEqual((usage.from_plan, usage.overage, s.used, s.overage, s.remaining), (100, 20, 100, 20, 0))
        self.assertTrue(s.allows(5))
        entitlements.release(usage)
        s = entitlements.status(self.org, "exports")
        self.assertEqual((s.used, s.overage), (0, 0))

    def test_amount_must_be_positive_and_feature_metered(self):
        with self.assertRaises(ValueError):
            entitlements.consume(self.org, "exports", 0)
        with self.assertRaises(ValueError):
            entitlements.consume(self.org, "api")


class CreditTests(Base):
    def test_plan_first_then_credits_expiring_first(self):
        now = timezone.now()
        later = entitlements.grant(self.org, "exports", 5, expires_at=now + timedelta(days=60))
        never = entitlements.grant(self.org, "exports", 5)
        soon = entitlements.grant(self.org, "exports", 5, expires_at=now + timedelta(days=5))
        usage = entitlements.consume(self.org, "exports", 10)
        self.assertEqual(usage.from_plan, 3)
        self.assertEqual(usage.from_grants, [[soon.pk, 5], [later.pk, 2]])
        later.refresh_from_db()
        never.refresh_from_db()
        self.assertEqual((later.remaining, never.remaining), (3, 5))

    def test_expired_and_future_credits_dont_count(self):
        now = timezone.now()
        entitlements.grant(self.org, "exports", 5, expires_at=now - timedelta(seconds=1))
        entitlements.grant(self.org, "exports", 5, starts_at=now + timedelta(days=1))
        self.assertEqual(entitlements.status(self.org, "exports").credits, 0)

    def test_credits_survive_plan_changes_and_resets(self):
        entitlements.grant(self.org, "exports", 4)
        entitlements.subscribe(self.org, "team")
        entitlements.subscribe(self.org, "free")
        with time_machine.travel(timezone.now() + timedelta(days=40), tick=False):
            self.assertEqual(entitlements.status(self.org, "exports").remaining, 3 + 4)

    def test_grant_with_key_is_idempotent(self):
        a = entitlements.grant(self.org, "exports", 10, key="pi_123")
        b = entitlements.grant(self.org, "exports", 10, key="pi_123")
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(entitlements.status(self.org, "exports").credits, 10)

    def test_credits_only_for_metered(self):
        with self.assertRaises(ValueError):
            entitlements.grant(self.org, "api", 1)


class SubscriptionTests(Base):
    def test_new_base_plan_replaces_the_old_one(self):
        entitlements.subscribe(self.org, "team")
        entitlements.subscribe(self.org, "pro")
        self.assertEqual([s.plan.key for s in entitlements.subscriptions(self.org)], ["pro"])

    def test_changing_plan_keeps_the_periods_usage(self):
        entitlements.subscribe(self.org, "team")
        entitlements.consume(self.org, "exports", 40)
        entitlements.unsubscribe(self.org)
        entitlements.subscribe(self.org, "team")
        self.assertEqual(entitlements.status(self.org, "exports").remaining, 10)
        entitlements.subscribe(self.org, "free")
        self.assertEqual(entitlements.status(self.org, "exports").remaining, 0)

    def test_addons_stack_and_quantity_multiplies(self):
        entitlements.subscribe(self.org, "team")
        entitlements.subscribe(self.org, "seats", quantity=3)
        s = entitlements.status(self.org, "exports")
        self.assertEqual(s.limit, 50 + 3 * 10)
        self.assertEqual(entitlements.status(self.org, "projects").limit, 10 + 3 * 2)
        entitlements.subscribe(self.org, "pro")  # unlimited projects wins
        self.assertIsNone(entitlements.status(self.org, "projects").limit)
        self.assertEqual(len(entitlements.subscriptions(self.org)), 2)

    def test_addon_on_top_of_the_default_plan(self):
        entitlements.subscribe(self.org, "seats")
        self.assertEqual(entitlements.status(self.org, "exports").limit, 3 + 10)

    def test_ended_subscription_falls_back_to_default(self):
        entitlements.subscribe(self.org, "pro", ends_at=timezone.now() + timedelta(days=30))
        self.assertTrue(entitlements.can(self.org, "api"))
        with time_machine.travel(timezone.now() + timedelta(days=31), tick=False):
            self.assertFalse(entitlements.can(self.org, "api"))
            self.assertEqual(entitlements.status(self.org, "exports").limit, 3)

    def test_unsubscribe_at_period_end(self):
        entitlements.subscribe(self.org, "pro")
        end = timezone.now() + timedelta(days=10)
        self.assertEqual(entitlements.unsubscribe(self.org, at=end), 1)
        self.assertTrue(entitlements.can(self.org, "api"))
        with time_machine.travel(end, tick=False):
            self.assertFalse(entitlements.can(self.org, "api"))

    def test_external_id_upserts(self):
        entitlements.subscribe(self.org, "team", external_id="sub_1", source="stripe")
        entitlements.subscribe(self.org, "pro", external_id="sub_1", quantity=2, source="stripe")
        self.assertEqual(Subscription.objects.count(), 1)
        sub = Subscription.objects.get()
        self.assertEqual((sub.plan.key, sub.quantity), ("pro", 2))
        self.assertEqual(entitlements.unsubscribe(external_id="sub_1"), 1)
        self.assertFalse(entitlements.can(self.org, "api"))

    def test_future_subscription_starts_later(self):
        start = timezone.now() + timedelta(days=3)
        entitlements.subscribe(self.org, "pro", starts_at=start)
        self.assertFalse(entitlements.can(self.org, "api"))
        with time_machine.travel(start, tick=False):
            self.assertTrue(entitlements.can(self.org, "api"))

    def test_subjects_are_separate(self):
        other = Org.objects.create(name="other")
        entitlements.subscribe(self.org, "pro")
        self.assertFalse(entitlements.can(other, "api"))


class OverrideTests(Base):
    def test_override_replaces_plan_until_it_ends(self):
        entitlements.subscribe(self.org, "pro")
        end = timezone.now() + timedelta(days=1)
        entitlements.override(self.org, "api", enabled=False, ends_at=end)
        self.assertFalse(entitlements.can(self.org, "api"))
        with time_machine.travel(end, tick=False):
            self.assertTrue(entitlements.can(self.org, "api"))

    def test_override_can_unlock_a_feature(self):
        entitlements.override(self.org, "ai_minutes", limit=5)
        entitlements.consume(self.org, "ai_minutes", 5)
        self.assertFalse(entitlements.can(self.org, "ai_minutes"))


class LimitTests(Base):
    def test_counts_live_objects(self):
        self.assertTrue(entitlements.can(self.org, "projects"))
        Project.objects.create(org=self.org)
        s = entitlements.status(self.org, "projects")
        self.assertEqual((s.used, s.limit, s.remaining), (1, 1, 0))
        with self.assertRaises(Denied):
            entitlements.check(self.org, "projects")
        Project.objects.all().delete()
        entitlements.check(self.org, "projects")

    def test_check_inside_a_transaction(self):
        with transaction.atomic():
            entitlements.check(self.org, "projects")
            Project.objects.create(org=self.org)

    def test_switch(self):
        with self.assertRaises(Denied) as ctx:
            entitlements.check(self.org, "api")
        self.assertEqual(ctx.exception.reason, "disabled")
        entitlements.subscribe(self.org, "team")
        self.assertTrue(entitlements.check(self.org, "api").enabled)


class LedgerTests(Base):
    def test_consume_with_key_is_idempotent(self):
        a = entitlements.consume(self.org, "exports", 2, key="job-1")
        b = entitlements.consume(self.org, "exports", 2, key="job-1")
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(entitlements.status(self.org, "exports").used, 2)

    def test_release_once_back_to_where_it_came_from(self):
        g = entitlements.grant(self.org, "exports", 5)
        usage = entitlements.consume(self.org, "exports", 5)
        self.assertTrue(entitlements.release(usage))
        self.assertFalse(entitlements.release(usage))
        g.refresh_from_db()
        s = entitlements.status(self.org, "exports")
        self.assertEqual((s.used, g.remaining), (0, 5))

    def test_release_after_a_reset_goes_to_the_old_period(self):
        usage = entitlements.consume(self.org, "exports", 3)
        with time_machine.travel(timezone.now() + timedelta(days=40), tick=False):
            entitlements.consume(self.org, "exports", 3)
            entitlements.release(usage)
            self.assertEqual(entitlements.status(self.org, "exports").used, 3)

    def test_use_releases_when_the_block_fails(self):
        with self.assertRaises(RuntimeError), entitlements.use(self.org, "exports"):
            raise RuntimeError("export failed")
        with entitlements.use(self.org, "exports"):
            pass
        self.assertEqual(entitlements.status(self.org, "exports").used, 1)
        self.assertEqual(Usage.objects.filter(released_at__isnull=False).count(), 1)

    def test_report_and_forget(self):
        entitlements.consume(self.org, "exports")
        Project.objects.create(org=self.org)
        report = entitlements.report(self.org)
        self.assertEqual(set(report), set(CATALOG["features"]))
        self.assertEqual(report["exports"].used, 1)
        entitlements.forget(self.org)
        self.assertEqual(entitlements.status(self.org, "exports").used, 0)
        self.assertFalse(Counter.objects.exists())

    def test_unsaved_subject_is_refused(self):
        with self.assertRaises(ValueError):
            entitlements.can(Org(name="unsaved"), "exports")


class ViewTests(Base):
    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import User

        self.user = User.objects.create(username="ada")
        self.client.force_login(self.user)

    def test_requires_consumes_then_answers_403(self):
        for _ in range(3):
            self.assertEqual(self.client.get("/export/").status_code, 200)
        self.assertEqual(self.client.get("/export/").status_code, 403)

    def test_failed_response_releases(self):
        self.assertEqual(self.client.get("/export/?status=500").status_code, 500)
        self.assertEqual(entitlements.status(self.user, "exports").used, 0)

    def test_switch_view(self):
        self.assertEqual(self.client.get("/api/").status_code, 403)
        entitlements.subscribe(self.user, "pro")
        self.assertEqual(self.client.get("/api/").status_code, 200)


class AdminTests(Base):
    def test_admin_pages_load(self):
        from django.contrib.auth.models import User

        self.client.force_login(User.objects.create_superuser("admin", "a@example.com", "x"))
        entitlements.subscribe(self.org, "pro")
        entitlements.consume(self.org, "exports")
        for url in [
            "/admin/entitlements/plan/",
            f"/admin/entitlements/plan/{Plan.objects.get(key='pro').pk}/change/",
            "/admin/entitlements/subscriber/",
            f"/admin/entitlements/subscriber/{Subscription.objects.get().subscriber_id}/change/",
            "/admin/entitlements/usage/",
            "/admin/entitlements/feature/",
        ]:
            self.assertEqual(self.client.get(url).status_code, 200, url)


class ConcurrencyTests(TransactionTestCase):
    """Many requests at once must never consume more than exists. Needs a real database server."""

    def setUp(self):
        if connection.vendor == "sqlite":
            self.skipTest("run with DATABASE=postgres")
        entitlements.define(CATALOG)
        self.org = Org.objects.create(name="acme")

    def hammer(self, fn, n=20):
        ok, barrier = [], threading.Barrier(n)

        def worker():
            barrier.wait()
            try:
                fn()
                ok.append(1)
            except Denied:
                pass
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return len(ok)

    def test_parallel_consumption_never_overspends(self):
        entitlements.subscribe(self.org, "team")  # 50 a month
        entitlements.grant(self.org, "exports", 7)
        entitlements.grant(self.org, "exports", 3, expires_at=timezone.now() + timedelta(days=1))
        self.assertEqual(self.hammer(lambda: entitlements.consume(self.org, "exports", 3), n=30), 20)
        s = entitlements.status(self.org, "exports")
        self.assertEqual((s.used, s.credits), (50, 0))
        self.assertEqual(sum(g.remaining for g in Grant.objects.all()), 0)

    def test_parallel_same_key_consumes_once(self):
        self.assertEqual(self.hammer(lambda: entitlements.consume(self.org, "exports", 1, key="k")), 20)
        self.assertEqual(Usage.objects.count(), 1)
        self.assertEqual(entitlements.status(self.org, "exports").used, 1)

    def test_parallel_first_touch_creates_one_subscriber(self):
        self.hammer(lambda: entitlements.grant(self.org, "exports", 1))
        self.assertEqual(entitlements.status(self.org, "exports").credits, 20)

    def test_check_serializes_limit_features(self):
        entitlements.subscribe(self.org, "team")  # 10 projects

        def create():
            with transaction.atomic():
                entitlements.check(self.org, "projects")
                Project.objects.create(org=self.org)

        self.assertEqual(self.hammer(create), 10)
        self.assertEqual(self.org.projects.count(), 10)
