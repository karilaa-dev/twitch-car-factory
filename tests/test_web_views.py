from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from controller.models import (
    AccountCustomChannel,
    AccountChannelSelection,
    ActionLog,
    ChannelPreset,
    MinerAccount,
    MinerCommand,
    MinerIncident,
    MinerInstanceState,
    MinerRun,
    PresetChannel,
    WorkerLease,
)


class WebViewTests(TestCase):
    password = "operator-test-password"

    def setUp(self):
        user_model = get_user_model()
        self.staff = user_model.objects.create_user(
            username="night-operator",
            password=self.password,
            is_staff=True,
        )
        self.account = MinerAccount.objects.create(
            config_key="primary",
            display_username="primary_twitch",
            is_configured=True,
        )
        AccountChannelSelection.objects.create(account=self.account)
        self.state = MinerInstanceState.objects.create(
            account=self.account,
            desired_state=MinerInstanceState.DesiredState.STOPPED,
            observed_state=MinerInstanceState.ObservedState.STOPPED,
        )

    def login(self):
        self.client.force_login(self.staff)

    def create_run(self, *, channels=None, active=True):
        run = MinerRun.objects.create(
            account=self.account,
            source_mode=AccountChannelSelection.Mode.DEFAULT,
            source_name="config.yaml",
            channels=channels or ["warframe", "twitch"],
            configuration_fingerprint="a" * 64,
            channel_revision=self.account.channel_revision,
            pid=4321,
            startup_confirmed_at=timezone.now(),
            ended_at=None if active else timezone.now(),
        )
        if active:
            self.state.current_run = run
            self.state.advisory_pid = run.pid
            self.state.desired_state = MinerInstanceState.DesiredState.RUNNING
            self.state.observed_state = MinerInstanceState.ObservedState.RUNNING
            self.state.last_heartbeat = timezone.now()
            self.state.save()
        return run

    def create_preset(self, name="Drops", channels=None):
        preset = ChannelPreset.objects.create(name=name)
        for position, channel in enumerate(channels or ["channel_one", "channel_two"]):
            PresetChannel.objects.create(
                preset=preset,
                position=position,
                name=channel,
            )
        return preset

    def test_healthz_is_public_and_discloses_no_farm_state(self):
        response = self.client.get(reverse("controller:healthz"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok\n")
        self.assertNotContains(response, self.account.config_key)

    @override_settings(
        ALLOWED_HOSTS=["farm.example.com"],
        SECURE_SSL_REDIRECT=True,
    )
    def test_healthz_accepts_the_configured_production_host(self):
        response = self.client.get(
            reverse("controller:healthz"),
            HTTP_HOST="farm.example.com",
            HTTP_X_FORWARDED_PROTO="https",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok\n")

    def test_every_control_surface_requires_staff(self):
        preset = self.create_preset()
        protected_urls = [
            reverse("controller:dashboard"),
            reverse("controller:status_fragment"),
            reverse("controller:account_list"),
            reverse("controller:account_detail", args=[self.account.pk]),
            reverse("controller:preset_list"),
            reverse("controller:preset_create"),
            reverse("controller:preset_detail", args=[preset.pk]),
            reverse("controller:preset_edit", args=[preset.pk]),
        ]

        for url in protected_urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("controller:login"), response.url)

    def test_login_rejects_authenticated_non_staff_accounts(self):
        non_staff = get_user_model().objects.create_user(
            username="viewer",
            password=self.password,
        )

        response = self.client.post(
            reverse("controller:login"),
            {"username": non_staff.username, "password": self.password},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "restricted to staff operators")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_staff_login_and_post_only_logout(self):
        response = self.client.post(
            reverse("controller:login"),
            {"username": self.staff.username, "password": self.password},
        )
        self.assertRedirects(response, reverse("controller:dashboard"))

        self.assertEqual(self.client.get(reverse("controller:logout")).status_code, 405)
        response = self.client.post(reverse("controller:logout"))
        self.assertRedirects(response, reverse("controller:login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_dashboard_renders_desired_observed_exact_channels_and_incident(self):
        self.login()
        run = self.create_run(channels=["exact_first", "exact_second"])
        incident = MinerIncident.objects.create(
            account=self.account,
            run=run,
            kind=MinerIncident.Kind.UNEXPECTED_EXIT,
            summary="Process exited between health polls",
        )
        self.account.refresh_from_db()
        WorkerLease.objects.create(
            owner_id="worker-test",
            pid=987,
            expires_at=timezone.now() + timedelta(seconds=30),
        )

        response = self.client.get(reverse("controller:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Desired")
        self.assertContains(response, "Observed")
        self.assertContains(response, "exact_first")
        self.assertContains(response, "exact_second")
        self.assertContains(response, incident.summary)
        self.assertContains(response, "Supervisor online")

    def test_status_fragment_is_authenticated_and_contains_no_page_shell(self):
        self.login()
        self.create_run(channels=["fragment_channel"])

        response = self.client.get(reverse("controller:status_fragment"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="live-status"')
        self.assertContains(response, "fragment_channel")
        self.assertNotContains(response, "<!doctype html>")

    def test_supervisor_staleness_is_a_critical_dashboard_alert(self):
        self.login()
        WorkerLease.objects.create(
            owner_id="dead-worker",
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        response = self.client.get(reverse("controller:dashboard"))

        self.assertContains(response, "Supervisor heartbeat stale")
        self.assertContains(response, "no live supervisor currently owns process reconciliation")

    def test_lifecycle_mutations_are_post_only_and_audit_the_actor(self):
        self.login()
        start_url = reverse(
            "controller:account_action",
            args=[self.account.pk, MinerCommand.Action.START],
        )
        self.assertEqual(self.client.get(start_url).status_code, 405)

        response = self.client.post(start_url)

        self.assertRedirects(
            response,
            reverse("controller:account_detail", args=[self.account.pk]),
        )
        self.state.refresh_from_db()
        command = MinerCommand.objects.get()
        self.assertEqual(self.state.desired_state, MinerInstanceState.DesiredState.RUNNING)
        self.assertEqual(command.action, MinerCommand.Action.START)
        self.assertEqual(command.actor, self.staff)
        self.assertTrue(ActionLog.objects.filter(actor=self.staff, action="miner_start").exists())

    def test_manual_stop_is_durable_and_does_not_create_an_incident(self):
        self.login()
        self.state.desired_state = MinerInstanceState.DesiredState.RUNNING
        self.state.save()

        self.client.post(
            reverse(
                "controller:account_action",
                args=[self.account.pk, MinerCommand.Action.STOP],
            )
        )

        self.state.refresh_from_db()
        self.assertEqual(self.state.desired_state, MinerInstanceState.DesiredState.STOPPED)
        self.assertEqual(MinerIncident.objects.count(), 0)
        self.assertTrue(
            MinerCommand.objects.filter(
                account=self.account,
                action=MinerCommand.Action.STOP,
            ).exists()
        )

    def test_global_action_is_post_only_and_targets_configured_accounts(self):
        self.login()
        disabled = MinerAccount.objects.create(
            config_key="orphaned",
            display_username="old_account",
            is_configured=False,
        )
        AccountChannelSelection.objects.create(account=disabled)
        MinerInstanceState.objects.create(account=disabled)
        url = reverse("controller:global_action", args=[MinerCommand.Action.START])

        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url)

        self.assertRedirects(response, reverse("controller:dashboard"))
        self.assertEqual(MinerCommand.objects.count(), 1)
        self.assertEqual(MinerCommand.objects.get().account, self.account)

    def test_mutation_rejects_missing_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)

        response = client.post(
            reverse("controller:global_action", args=[MinerCommand.Action.STOP])
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(MinerCommand.objects.count(), 0)

    def test_custom_channel_change_normalizes_and_queues_restart(self):
        self.login()
        self.state.desired_state = MinerInstanceState.DesiredState.RUNNING
        self.state.save()
        old_revision = self.account.channel_revision

        response = self.client.post(
            reverse("controller:account_channel_selection", args=[self.account.pk]),
            {
                "mode": AccountChannelSelection.Mode.CUSTOM,
                "preset": "",
                "custom_channels": "Warframe, warframe\nTwitch",
            },
        )

        self.assertRedirects(
            response,
            reverse("controller:account_detail", args=[self.account.pk]),
        )
        selection = AccountChannelSelection.objects.get(account=self.account)
        self.account.refresh_from_db()
        self.assertEqual(selection.mode, AccountChannelSelection.Mode.CUSTOM)
        self.assertEqual(
            list(self.account.custom_channels.values_list("name", flat=True)),
            ["Warframe", "Twitch"],
        )
        self.assertEqual(self.account.channel_revision, old_revision + 1)
        self.assertTrue(
            MinerCommand.objects.filter(action=MinerCommand.Action.RESTART).exists()
        )

    def test_invalid_custom_channels_do_not_change_a_healthy_unit(self):
        self.login()
        current_run = self.create_run(channels=["known_good"])
        old_revision = self.account.channel_revision

        response = self.client.post(
            reverse("controller:account_channel_selection", args=[self.account.pk]),
            {
                "mode": AccountChannelSelection.Mode.CUSTOM,
                "preset": "",
                "custom_channels": "",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Add at least one channel", status_code=400)
        self.account.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual(self.account.channel_revision, old_revision)
        self.assertEqual(self.state.current_run, current_run)
        self.assertEqual(self.state.desired_state, MinerInstanceState.DesiredState.RUNNING)
        self.assertEqual(MinerCommand.objects.count(), 0)

    def test_default_and_preset_modes_ignore_invalid_unused_custom_draft(self):
        self.login()
        preset = self.create_preset(name="Weekend")
        selection = self.account.selection
        selection.mode = AccountChannelSelection.Mode.CUSTOM
        selection.save(update_fields=("mode", "updated_at"))
        AccountCustomChannel.objects.create(
            account=self.account,
            position=0,
            name="saved_channel",
        )
        old_revision = self.account.channel_revision

        cases = (
            (AccountChannelSelection.Mode.DEFAULT, "", None),
            (AccountChannelSelection.Mode.PRESET, str(preset.pk), preset.pk),
        )
        for mode, preset_value, expected_preset_id in cases:
            with self.subTest(mode=mode):
                selection.mode = AccountChannelSelection.Mode.CUSTOM
                selection.preset = None
                selection.save(update_fields=("mode", "preset", "updated_at"))

                response = self.client.post(
                    reverse("controller:account_channel_selection", args=[self.account.pk]),
                    {
                        "mode": mode,
                        "preset": preset_value,
                        "custom_channels": "invalid unused draft!",
                    },
                )

                self.assertRedirects(
                    response,
                    reverse("controller:account_detail", args=[self.account.pk]),
                )
                selection.refresh_from_db()
                self.assertEqual(selection.mode, mode)
                self.assertEqual(selection.preset_id, expected_preset_id)
                self.assertEqual(
                    list(self.account.custom_channels.values_list("name", flat=True)),
                    ["saved_channel"],
                )

        self.account.refresh_from_db()
        self.assertEqual(self.account.channel_revision, old_revision + len(cases))

    def test_custom_mode_still_rejects_invalid_channel_names(self):
        self.login()
        current_run = self.create_run(channels=["known_good"])
        old_revision = self.account.channel_revision

        response = self.client.post(
            reverse("controller:account_channel_selection", args=[self.account.pk]),
            {
                "mode": AccountChannelSelection.Mode.CUSTOM,
                "preset": "",
                "custom_channels": "valid_channel\ninvalid channel!",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Invalid Twitch channel name", status_code=400)
        self.account.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual(self.account.channel_revision, old_revision)
        self.assertEqual(self.state.current_run, current_run)
        self.assertEqual(self.state.desired_state, MinerInstanceState.DesiredState.RUNNING)
        self.assertEqual(MinerCommand.objects.count(), 0)

    def test_preset_create_deduplicates_channels_and_records_action(self):
        self.login()

        response = self.client.post(
            reverse("controller:preset_create"),
            {"name": " Weekend ", "channels": "One, two\nONE"},
        )

        preset = ChannelPreset.objects.get(name="Weekend")
        self.assertRedirects(
            response,
            reverse("controller:preset_detail", args=[preset.pk]),
        )
        self.assertEqual(preset.channel_names, ["One", "two"])
        self.assertTrue(ActionLog.objects.filter(action="preset_created").exists())

    def test_editing_assigned_preset_restarts_desired_running_account(self):
        self.login()
        preset = self.create_preset()
        selection = self.account.selection
        selection.mode = AccountChannelSelection.Mode.PRESET
        selection.preset = preset
        selection.save()
        self.state.desired_state = MinerInstanceState.DesiredState.RUNNING
        self.state.save()

        response = self.client.post(
            reverse("controller:preset_edit", args=[preset.pk]),
            {"name": "Drops v2", "channels": "new_one\nnew_two"},
        )

        self.assertRedirects(
            response,
            reverse("controller:preset_detail", args=[preset.pk]),
        )
        preset.refresh_from_db()
        self.assertEqual(preset.name, "Drops v2")
        self.assertEqual(preset.channel_names, ["new_one", "new_two"])
        self.assertTrue(
            MinerCommand.objects.filter(
                account=self.account,
                action=MinerCommand.Action.RESTART,
            ).exists()
        )

    def test_preset_assignment_updates_both_selected_and_deselected_accounts(self):
        self.login()
        preset = self.create_preset()
        second = MinerAccount.objects.create(
            config_key="second",
            display_username="second_twitch",
            is_configured=True,
        )
        second_selection = AccountChannelSelection.objects.create(
            account=second,
            mode=AccountChannelSelection.Mode.PRESET,
            preset=preset,
        )
        MinerInstanceState.objects.create(account=second)

        response = self.client.post(
            reverse("controller:preset_assign", args=[preset.pk]),
            {"accounts": [str(self.account.pk)]},
        )

        self.assertRedirects(
            response,
            reverse("controller:preset_detail", args=[preset.pk]),
        )
        self.account.selection.refresh_from_db()
        second_selection.refresh_from_db()
        self.assertEqual(self.account.selection.mode, AccountChannelSelection.Mode.PRESET)
        self.assertEqual(self.account.selection.preset, preset)
        self.assertEqual(second_selection.mode, AccountChannelSelection.Mode.DEFAULT)
        self.assertIsNone(second_selection.preset)

    def test_assigned_preset_delete_is_protected(self):
        self.login()
        preset = self.create_preset()
        selection = self.account.selection
        selection.mode = AccountChannelSelection.Mode.PRESET
        selection.preset = preset
        selection.save()

        response = self.client.post(
            reverse("controller:preset_delete", args=[preset.pk])
        )

        self.assertRedirects(
            response,
            reverse("controller:preset_detail", args=[preset.pk]),
        )
        self.assertTrue(ChannelPreset.objects.filter(pk=preset.pk).exists())

    def test_unassigned_preset_delete_is_post_only_and_audited(self):
        self.login()
        preset = self.create_preset()
        url = reverse("controller:preset_delete", args=[preset.pk])

        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url)

        self.assertRedirects(response, reverse("controller:preset_list"))
        self.assertFalse(ChannelPreset.objects.filter(pk=preset.pk).exists())
        self.assertTrue(ActionLog.objects.filter(action="preset_deleted").exists())

    def test_pages_never_render_credentials(self):
        self.login()
        response = self.client.get(
            reverse("controller:account_detail", args=[self.account.pk])
        )

        self.assertNotContains(response, "operator-test-password")
