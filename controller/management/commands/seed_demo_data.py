"""Populate a development database with representative control-room data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from controller.crypto import encrypt_text
from controller.models import (
    AccountChannelSelection,
    AccountCredential,
    AccountCustomChannel,
    ChannelPreset,
    FarmConfiguration,
    MinerAccount,
    MinerIncident,
    MinerInstanceState,
    MinerRun,
    PresetChannel,
)


@dataclass(frozen=True, slots=True)
class DemoAccount:
    state: str
    desired: str
    mode: str
    channels: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"demo-{self.state}"


DEMO_ACCOUNTS = (
    DemoAccount("starting", "running", "default", ("warframe", "twitch")),
    DemoAccount("running", "running", "preset", ("lirik", "cohhcarnage", "shroud")),
    DemoAccount("stopping", "stopped", "custom", ("sodapoppin", "moonmoon")),
    DemoAccount("stopped", "stopped", "default", ("warframe", "twitch")),
    DemoAccount("restarting", "running", "preset", ("lirik", "cohhcarnage", "shroud")),
    DemoAccount("degraded", "running", "custom", ("esl_csgo", "riotgames")),
    DemoAccount("unknown", "stopped", "default", ("warframe", "twitch")),
)


class Command(BaseCommand):
    help = "Seed development-only fake accounts covering every observed runtime state."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo data can only be seeded while DJANGO_DEBUG is enabled.")

        with transaction.atomic():
            configuration = FarmConfiguration.load()
            if not configuration.default_channels:
                configuration.default_channels = ["warframe", "twitch"]
                configuration.save(update_fields=("default_channels", "updated_at"))

            preset, _ = ChannelPreset.objects.get_or_create(name="Demo drops rotation")
            if not preset.channels.exists():
                for position, name in enumerate(("lirik", "cohhcarnage", "shroud")):
                    PresetChannel.objects.create(
                        preset=preset,
                        position=position,
                        name=name,
                    )

            for index, demo in enumerate(DEMO_ACCOUNTS, start=1):
                self._seed_account(demo, preset=preset, index=index)

        states = ", ".join(demo.state for demo in DEMO_ACCOUNTS)
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(DEMO_ACCOUNTS)} fake accounts covering: {states}."
            )
        )

    @staticmethod
    def _seed_account(demo: DemoAccount, *, preset: ChannelPreset, index: int) -> None:
        account, created = MinerAccount.objects.get_or_create(
            config_key=demo.key,
            defaults={"display_username": f"demo_{demo.state}", "is_active": True},
        )
        if not created and not account.display_username.startswith("demo_"):
            raise CommandError(
                f"Refusing to replace non-demo account using reserved key {demo.key!r}."
            )

        account.display_username = f"demo_{demo.state}"
        account.is_active = True
        account.save(update_fields=("display_username", "is_active", "updated_at"))
        AccountCredential.objects.update_or_create(
            account=account,
            defaults={"password_ciphertext": encrypt_text("demo-password-not-for-twitch")},
        )

        selection, _ = AccountChannelSelection.objects.get_or_create(account=account)
        selection.mode = demo.mode
        selection.preset = preset if demo.mode == AccountChannelSelection.Mode.PRESET else None
        selection.save(update_fields=("mode", "preset", "updated_at"))
        account.custom_channels.all().delete()
        if demo.mode == AccountChannelSelection.Mode.CUSTOM:
            AccountCustomChannel.objects.bulk_create(
                [
                    AccountCustomChannel(account=account, position=position, name=name)
                    for position, name in enumerate(demo.channels)
                ]
            )

        now = timezone.now()
        state, _ = MinerInstanceState.objects.get_or_create(account=account)
        run = state.current_run
        needs_run = demo.state not in {
            MinerInstanceState.ObservedState.STOPPED,
            MinerInstanceState.ObservedState.UNKNOWN,
        }
        has_live_run = needs_run and demo.state != MinerInstanceState.ObservedState.DEGRADED
        if needs_run and run is None:
            run = account.runs.filter(worker_id="demo-supervisor").first()
        if needs_run and run is None:
            run = MinerRun.objects.create(
                account=account,
                source_mode=demo.mode,
                source_name=(
                    preset.name
                    if demo.mode == AccountChannelSelection.Mode.PRESET
                    else demo.key
                    if demo.mode == AccountChannelSelection.Mode.CUSTOM
                    else "farm defaults"
                ),
                channels=list(demo.channels),
                configuration_fingerprint=f"{index:064x}",
                channel_revision=account.channel_revision,
                pid=4100 + index,
                worker_id="demo-supervisor",
                startup_confirmed_at=(
                    None
                    if demo.state == MinerInstanceState.ObservedState.STARTING
                    else now - timedelta(minutes=8)
                ),
                started_at=now - timedelta(minutes=10 + index),
            )
        if run is not None:
            MinerRun.objects.filter(pk=run.pk).update(
                ended_at=(
                    now - timedelta(minutes=2)
                    if demo.state == MinerInstanceState.ObservedState.DEGRADED
                    else None
                ),
                exit_code=(1 if demo.state == MinerInstanceState.ObservedState.DEGRADED else None),
                stop_reason=(
                    MinerRun.StopReason.UNEXPECTED_EXIT
                    if demo.state == MinerInstanceState.ObservedState.DEGRADED
                    else ""
                ),
                error=(
                    "Fake miner exited unexpectedly."
                    if demo.state == MinerInstanceState.ObservedState.DEGRADED
                    else ""
                ),
            )
            run.refresh_from_db()
        if not needs_run:
            run = None

        state.desired_state = demo.desired
        state.observed_state = demo.state
        state.current_run = run if has_live_run else None
        state.advisory_pid = run.pid if has_live_run else None
        state.worker_id = "demo-supervisor" if has_live_run else ""
        state.retry_count = 2 if demo.state == MinerInstanceState.ObservedState.DEGRADED else 0
        state.next_retry_at = (
            now + timedelta(seconds=45)
            if demo.state == MinerInstanceState.ObservedState.DEGRADED
            else None
        )
        state.stable_since = (
            now - timedelta(minutes=8)
            if demo.state == MinerInstanceState.ObservedState.RUNNING
            else None
        )
        state.last_heartbeat = now - timedelta(seconds=index * 3) if run else None
        state.last_error = (
            "Fake miner exited unexpectedly; automatic recovery is scheduled."
            if demo.state == MinerInstanceState.ObservedState.DEGRADED
            else ""
        )
        state.save()

        if demo.state == MinerInstanceState.ObservedState.DEGRADED:
            MinerIncident.objects.update_or_create(
                account=account,
                status=MinerIncident.Status.OPEN,
                defaults={
                    "run": run,
                    "kind": MinerIncident.Kind.UNEXPECTED_EXIT,
                    "summary": "Fake miner exited unexpectedly",
                    "details": "Development fixture demonstrating automatic recovery.",
                    "opened_at": now - timedelta(minutes=2),
                },
            )
        else:
            MinerIncident.objects.filter(
                account=account,
                status=MinerIncident.Status.OPEN,
            ).delete()
