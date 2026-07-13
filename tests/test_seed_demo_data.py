from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from controller.models import MinerAccount, MinerIncident, MinerInstanceState


class SeedDemoDataTests(TestCase):
    @override_settings(DEBUG=True)
    def test_seeds_every_observed_state_and_is_repeatable(self):
        output = StringIO()

        call_command("seed_demo_data", stdout=output)
        call_command("seed_demo_data", stdout=output)

        demo_states = set(
            MinerInstanceState.objects.filter(account__config_key__startswith="demo-")
            .values_list("observed_state", flat=True)
        )
        self.assertEqual(demo_states, set(MinerInstanceState.ObservedState.values))
        self.assertEqual(
            MinerAccount.objects.filter(config_key__startswith="demo-").count(),
            len(MinerInstanceState.ObservedState.values),
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
        self.assertIn("Seeded 7 fake accounts", output.getvalue())

    @override_settings(DEBUG=False)
    def test_refuses_to_seed_outside_debug_mode(self):
        with self.assertRaisesMessage(CommandError, "DJANGO_DEBUG"):
            call_command("seed_demo_data")
