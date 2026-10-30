from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from entitlements.api import define


class Command(BaseCommand):
    help = "Create or update features and plans from settings.ENTITLEMENTS."

    def handle(self, *args, **options):
        catalog = getattr(settings, "ENTITLEMENTS", None)
        if not catalog:
            raise CommandError("settings.ENTITLEMENTS is not set. See the README for its format.")
        define(catalog)
        self.stdout.write(
            self.style.SUCCESS(
                f"Synced {len(catalog.get('features', {}))} features and {len(catalog.get('plans', {}))} plans."
            )
        )
