"""Synchronize non-secret account metadata from config.yaml."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from controller.config import ConfigError
from controller.services import sync_config_accounts


class Command(BaseCommand):
    help = "Mirror Twitch accounts from config.yaml without storing credentials."

    def add_arguments(self, parser):
        parser.add_argument("--config", help="Path to config.yaml.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and report changes, then roll the database transaction back.",
        )

    def handle(self, *args, **options):
        try:
            with transaction.atomic():
                result = sync_config_accounts(options.get("config"))
                if options["dry_run"]:
                    transaction.set_rollback(True)
        except ConfigError as exc:
            raise CommandError(str(exc)) from exc

        prefix = "Dry run: " if options["dry_run"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}created {len(result.created)}, updated {len(result.updated)}, "
                f"disabled {len(result.disabled)} account(s)."
            )
        )
