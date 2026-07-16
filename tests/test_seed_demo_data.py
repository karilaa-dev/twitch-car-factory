from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from controller.management.commands.seed_demo_data import DEFAULT_DEMO_PRESET_NAME
from controller.models import (
    AccountChannelSelection,
    MinerAccount,
    MinerIncident,
    MinerInstanceState,
)


class SeedDemoDataTests(TestCase):
    @override_settings(DEBUG=True)
    def test_seeds_every_observed_state_and_is_repeatable(self):
        output = StringIO()

        call_command("seed_demo_data", stdout=output)
        source_change = MinerAccount.objects.get(config_key="demo-source-change")
        stale_run = source_change.runtime_state.current_run
        type(stale_run).objects.filter(pk=stale_run.pk).update(
            source_mode=AccountChannelSelection.Mode.PRESET,
            source_name="Stale demo source",
            channels=["stalechannel"],
        )
        stale_run_pk = stale_run.pk
        call_command("seed_demo_data", stdout=output)

        demo_states = set(
            MinerInstanceState.objects.filter(account__config_key__startswith="demo-")
            .values_list("observed_state", flat=True)
        )
        self.assertEqual(demo_states, set(MinerInstanceState.ObservedState.values))
        self.assertEqual(
            MinerAccount.objects.filter(config_key__startswith="demo-").count(),
            len(MinerInstanceState.ObservedState.values) + 3,
        )
        degraded = MinerAccount.objects.get(config_key="demo-degraded")
        self.assertIsNone(degraded.runtime_state.current_run)
        self.assertEqual(degraded.runs.count(), 1)
        self.assertTrue(
            MinerIncident.objects.filter(
                account=degraded,
                status=MinerIncident.Status.OPEN,
            ).exists()
        )
        source_change = MinerAccount.objects.get(config_key="demo-source-change")
        current_run = source_change.runtime_state.current_run
        self.assertIsNotNone(current_run)
        self.assertNotEqual(current_run.pk, stale_run_pk)
        stale_run.refresh_from_db()
        self.assertIsNotNone(stale_run.ended_at)
        self.assertEqual(current_run.source_mode, AccountChannelSelection.Mode.CUSTOM)
        self.assertEqual(current_run.source_name, "demo-source-change")
        self.assertEqual(
            current_run.channels,
            ["lirik", "cohhcarnage", "shroud"],
        )
        self.assertEqual(
            source_change.selection.mode,
            AccountChannelSelection.Mode.CUSTOM,
        )
        self.assertEqual(
            list(
                source_change.custom_channels.order_by("position").values_list(
                    "name", flat=True
                )
            ),
            ["twitchgaming", "rocketleague"],
        )
        preset_content_change = MinerAccount.objects.get(
            config_key="demo-preset-content-change"
        )
        preset_run = preset_content_change.runtime_state.current_run
        self.assertEqual(preset_run.source_mode, AccountChannelSelection.Mode.PRESET)
        self.assertEqual(preset_run.source_name, "Demo evolving rotation")
        self.assertEqual(preset_run.channels, ["lirik", "cohhcarnage", "shroud"])
        self.assertEqual(
            preset_content_change.selection.mode,
            AccountChannelSelection.Mode.PRESET,
        )
        self.assertEqual(
            preset_content_change.selection.preset.name,
            "Demo evolving rotation",
        )
        self.assertEqual(
            preset_content_change.selection.preset.channel_names,
            ["lirik", "twitchgaming", "rocketleague"],
        )
        custom_to_preset = MinerAccount.objects.get(
            config_key="demo-custom-to-preset"
        )
        custom_run = custom_to_preset.runtime_state.current_run
        self.assertEqual(custom_run.source_mode, AccountChannelSelection.Mode.CUSTOM)
        self.assertEqual(custom_run.source_name, "demo-custom-to-preset")
        self.assertEqual(custom_run.channels, ["sodapoppin", "moonmoon"])
        self.assertEqual(
            custom_to_preset.selection.mode,
            AccountChannelSelection.Mode.PRESET,
        )
        self.assertEqual(
            custom_to_preset.selection.preset.name,
            DEFAULT_DEMO_PRESET_NAME,
        )
        self.assertIn("Seeded 10 fake accounts", output.getvalue())

    @override_settings(DEBUG=False)
    def test_refuses_to_seed_outside_debug_mode(self):
        with self.assertRaisesMessage(CommandError, "DJANGO_DEBUG"):
            call_command("seed_demo_data")
