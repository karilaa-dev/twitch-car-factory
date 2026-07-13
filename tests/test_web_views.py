from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from controller.crypto import decrypt_text, encrypt_text
from controller.models import (
    AccountCredential,
    AccountCustomChannel,
    AccountChannelSelection,
    ActionLog,
    ChannelPreset,
    FarmConfiguration,
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
            is_active=True,
        )
        self.twitch_password = "primary-twitch-password"
        self.credential = AccountCredential.objects.create(
            account=self.account,
            password_ciphertext=encrypt_text(self.twitch_password),
        )
        AccountChannelSelection.objects.create(account=self.account)
        self.configuration = FarmConfiguration.load()
        self.configuration.default_channels = ["warframe", "twitch"]
        self.configuration.autostart_new_accounts = False
        self.configuration.save()
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
            source_name="farm defaults",
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
            reverse("controller:account_create"),
            reverse("controller:account_detail", args=[self.account.pk]),
            reverse("controller:account_edit", args=[self.account.pk]),
            reverse("controller:account_info_fragment", args=[self.account.pk]),
            reverse("controller:bot_logs"),
            reverse("controller:bot_log_tail"),
            reverse("controller:preset_list"),
            reverse("controller:preset_create"),
            reverse("controller:preset_detail", args=[preset.pk]),
            reverse("controller:preset_edit", args=[preset.pk]),
            reverse("controller:settings_general"),
            reverse("controller:settings_import"),
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
        self.assertContains(response, "<th scope=\"col\">Status</th>")
        self.assertContains(response, "Default")
        self.assertContains(response, "exact_first")
        self.assertContains(response, "exact_second")
        self.assertContains(response, incident.summary)
        self.assertContains(response, "Supervisor online")
        self.assertNotContains(response, "Incident open")
        self.assertNotContains(response, "<th scope=\"col\">Process</th>")
        self.assertNotContains(response, "pid 4321")
        self.assertNotContains(response, "pid 987")

        detail = self.client.get(
            reverse("controller:account_detail", args=[self.account.pk])
        )
        self.assertContains(detail, "pid 4321")

    def test_status_fragment_is_authenticated_and_contains_no_page_shell(self):
        self.login()
        self.create_run(channels=["fragment_channel"])

        response = self.client.get(reverse("controller:status_fragment"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="live-status"')
        self.assertContains(response, "fragment_channel")
        self.assertNotContains(response, "<!doctype html>")

    def test_status_fragment_loads_farm_defaults_once_for_all_accounts(self):
        self.login()
        for index in range(3):
            account = MinerAccount.objects.create(
                config_key=f"default-{index}",
                display_username=f"default_user_{index}",
            )
            AccountChannelSelection.objects.create(account=account)

        with patch.object(
            FarmConfiguration,
            "load",
            wraps=FarmConfiguration.load,
        ) as load_configuration:
            response = self.client.get(reverse("controller:status_fragment"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(load_configuration.call_count, 1)

    def test_supervisor_staleness_is_a_critical_dashboard_alert(self):
        self.login()
        WorkerLease.objects.create(
            owner_id="dead-worker",
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        response = self.client.get(reverse("controller:dashboard"))

        self.assertContains(response, "Supervisor heartbeat stale")
        self.assertContains(response, "no live supervisor currently owns process reconciliation")

    def test_account_create_persists_encrypted_credentials_and_explicit_start(self):
        self.login()
        twitch_password = "new-account-secret"

        response = self.client.post(
            reverse("controller:account_create"),
            {
                "config_key": "secondary",
                "username": "secondary_twitch",
                "password": twitch_password,
                "mode": AccountChannelSelection.Mode.DEFAULT,
                "preset": "",
                "custom_channels": "",
                "start_after_save": "on",
            },
        )

        account = MinerAccount.objects.get(config_key="secondary")
        self.assertRedirects(
            response,
            reverse("controller:account_detail", args=[account.pk]),
        )
        self.assertTrue(account.is_active)
        self.assertEqual(account.display_username, "secondary_twitch")
        self.assertEqual(account.selection.mode, AccountChannelSelection.Mode.DEFAULT)
        self.assertNotEqual(account.credential.password_ciphertext, twitch_password)
        self.assertEqual(
            decrypt_text(account.credential.password_ciphertext),
            twitch_password,
        )
        self.assertEqual(
            account.runtime_state.desired_state,
            MinerInstanceState.DesiredState.RUNNING,
        )
        command = MinerCommand.objects.get(
            account=account,
            action=MinerCommand.Action.START,
        )
        self.assertEqual(command.actor, self.staff)
        action = ActionLog.objects.get(account=account, action="account_created")
        self.assertEqual(action.actor, self.staff)
        self.assertNotIn(twitch_password, str(action.details))

    def test_account_create_get_uses_general_settings_as_explicit_defaults(self):
        self.login()
        self.configuration.autostart_new_accounts = True
        self.configuration.save(update_fields=("autostart_new_accounts", "updated_at"))

        response = self.client.get(reverse("controller:account_create"))

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form["mode"].value(), AccountChannelSelection.Mode.DEFAULT)
        self.assertTrue(form["start_after_save"].value())
        self.assertContains(response, "data-channel-editor-root")
        self.assertContains(response, "data-preset-preview")

    def test_account_source_page_prefetches_ordered_preset_preview(self):
        self.login()
        preset = self.create_preset(
            name="Preview rotation",
            channels=["preview_one", "preview_two"],
        )

        response = self.client.get(
            reverse("controller:account_detail", args=[self.account.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-preset-id="%s"' % preset.pk)
        self.assertContains(response, "preview_one")
        self.assertContains(response, "preview_two")
        self.assertContains(response, "data-channel-editor-root")

    def test_invalid_account_form_never_echoes_submitted_password(self):
        self.login()
        submitted_password = "never-echo-invalid-form-secret"

        response = self.client.post(
            reverse("controller:account_create"),
            {
                "config_key": "invalid-account",
                "username": "",
                "password": submitted_password,
                "mode": AccountChannelSelection.Mode.DEFAULT,
                "preset": "",
                "custom_channels": "",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, submitted_password, status_code=400)

    @override_settings(DEBUG=True)
    def test_debug_error_report_redacts_account_password(self):
        submitted_password = "never-show-in-debug-report-secret"
        client = Client(raise_request_exception=False)
        client.force_login(self.staff)

        with patch(
            "controller.services.encrypt_text",
            side_effect=RuntimeError("forced credential storage failure"),
        ):
            response = client.post(
                reverse("controller:account_create"),
                {
                    "config_key": "debug-failure",
                    "username": "debug_failure_user",
                    "password": submitted_password,
                    "mode": AccountChannelSelection.Mode.DEFAULT,
                    "preset": "",
                    "custom_channels": "",
                },
            )

        self.assertEqual(response.status_code, 500)
        self.assertNotContains(response, submitted_password, status_code=500)

    def test_account_edit_keeps_key_replaces_secret_and_restarts_running_account(self):
        self.login()
        self.state.desired_state = MinerInstanceState.DesiredState.RUNNING
        self.state.save(update_fields=("desired_state", "updated_at"))
        replacement_password = "replacement-account-secret"

        response = self.client.post(
            reverse("controller:account_edit", args=[self.account.pk]),
            {
                "username": "renamed_twitch",
                "password": replacement_password,
                # A forged key must be ignored because it is not part of the edit form.
                "config_key": "forged-key",
            },
        )

        self.assertRedirects(
            response,
            reverse("controller:account_detail", args=[self.account.pk]),
        )
        self.account.refresh_from_db()
        self.credential.refresh_from_db()
        self.assertEqual(self.account.config_key, "primary")
        self.assertEqual(self.account.display_username, "renamed_twitch")
        self.assertEqual(
            decrypt_text(self.credential.password_ciphertext),
            replacement_password,
        )
        self.assertNotEqual(self.credential.password_ciphertext, replacement_password)
        self.assertTrue(
            MinerCommand.objects.filter(
                account=self.account,
                action=MinerCommand.Action.RESTART,
                actor=self.staff,
            ).exists()
        )
        action = ActionLog.objects.get(account=self.account, action="account_updated")
        self.assertEqual(
            action.details,
            {"username_changed": True, "credential_changed": True},
        )
        self.assertNotIn(replacement_password, str(action.details))

        edit_page = self.client.get(
            reverse("controller:account_edit", args=[self.account.pk])
        )
        self.assertNotContains(edit_page, self.twitch_password)
        self.assertNotContains(edit_page, replacement_password)
        self.assertNotContains(edit_page, self.credential.password_ciphertext)

    def test_archive_controls_are_removed_and_existing_inactive_rows_are_read_only(self):
        self.login()
        self.account.is_active = False
        self.account.save(update_fields=("is_active", "updated_at"))
        list_response = self.client.get(reverse("controller:account_list"))
        detail_response = self.client.get(
            reverse("controller:account_detail", args=[self.account.pk])
        )

        self.assertContains(list_response, "Inactive legacy record")
        self.assertNotContains(detail_response, "Archive account")
        self.assertNotContains(detail_response, "Reactivate account")
        self.assertEqual(
            self.client.get(reverse("controller:account_edit", args=[self.account.pk])).status_code,
            404,
        )
        self.assertEqual(self.client.post(f"/accounts/{self.account.pk}/archive/").status_code, 404)
        self.assertEqual(self.client.post(f"/accounts/{self.account.pk}/reactivate/").status_code, 404)

    def test_inactive_account_channel_selection_post_is_read_only(self):
        self.login()
        self.account.is_active = False
        self.account.save(update_fields=("is_active", "updated_at"))
        old_revision = self.account.channel_revision

        response = self.client.post(
            reverse("controller:account_channel_selection", args=[self.account.pk]),
            {
                "mode": AccountChannelSelection.Mode.CUSTOM,
                "preset": "",
                "custom_channels": "crafted_channel",
            },
        )

        self.assertEqual(response.status_code, 404)
        self.account.refresh_from_db()
        self.account.selection.refresh_from_db()
        self.assertEqual(self.account.selection.mode, AccountChannelSelection.Mode.DEFAULT)
        self.assertEqual(self.account.channel_revision, old_revision)
        self.assertFalse(self.account.custom_channels.exists())
        self.assertFalse(
            ActionLog.objects.filter(
                account=self.account,
                action="channel_selection_changed",
            ).exists()
        )

    def test_account_list_rows_link_to_open_without_an_edit_button(self):
        self.login()

        response = self.client.get(reverse("controller:account_list"))

        self.assertContains(
            response,
            f'<a class="button button--quiet" href="{reverse("controller:account_detail", args=[self.account.pk])}">Open</a>',
            html=True,
        )
        self.assertNotContains(
            response,
            f'<a class="button button--quiet" href="{reverse("controller:account_edit", args=[self.account.pk])}">Edit</a>',
            html=True,
        )

    def test_account_info_fragment_is_no_store_and_never_discloses_secrets(self):
        self.login()

        response = self.client.get(
            reverse("controller:account_info_fragment", args=[self.account.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertContains(response, self.account.display_username)
        self.assertContains(response, self.account.config_key)
        self.assertContains(response, "Credential")
        self.assertContains(response, "Present")
        self.assertContains(response, "Desired state")
        self.assertContains(response, "Observed state")
        self.assertContains(
            response,
            reverse("controller:account_edit", args=[self.account.pk]),
        )
        self.assertNotContains(response, self.twitch_password)
        self.assertNotContains(response, self.credential.password_ciphertext)

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
            is_active=False,
        )
        AccountChannelSelection.objects.create(account=disabled)
        MinerInstanceState.objects.create(account=disabled)
        url = reverse("controller:global_action", args=[MinerCommand.Action.START])

        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url)

        self.assertRedirects(response, reverse("controller:dashboard"))
        self.assertEqual(MinerCommand.objects.count(), 1)
        self.assertEqual(MinerCommand.objects.get().account, self.account)

        MinerCommand.objects.all().delete()
        response = self.client.post(
            reverse("controller:global_action", args=[MinerCommand.Action.RESTART])
        )
        self.assertRedirects(response, reverse("controller:dashboard"))
        self.assertEqual(MinerCommand.objects.get().action, MinerCommand.Action.RESTART)

    def test_bot_log_view_is_bounded_escaped_and_reports_worker_health(self):
        self.login()
        WorkerLease.objects.create(
            owner_id="worker-test",
            expires_at=timezone.now() + timedelta(seconds=30),
        )
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "twitch-farm.log"
            log_path.write_text("first line\n<script>alert(1)</script>\n", encoding="utf-8")
            with self.settings(TWITCH_FARM_LOG_FILE=log_path):
                response = self.client.get(reverse("controller:bot_logs"))
                tail = self.client.get(reverse("controller:bot_log_tail"))

        self.assertContains(response, "Supervisor online")
        self.assertContains(response, "first line")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(tail, 'data-bot-log-fragment')

    def test_mutation_rejects_missing_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)

        response = client.post(
            reverse("controller:global_action", args=[MinerCommand.Action.STOP])
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(MinerCommand.objects.count(), 0)

    def test_general_settings_update_database_defaults_and_restart_users(self):
        self.login()
        self.state.desired_state = MinerInstanceState.DesiredState.RUNNING
        self.state.save(update_fields=("desired_state", "updated_at"))
        old_revision = self.account.channel_revision

        response = self.client.post(
            reverse("controller:settings_general"),
            {
                "default_channels": "Warframe, warframe\nTwitchDrops",
                "autostart_new_accounts": "on",
            },
        )

        self.assertRedirects(response, reverse("controller:settings_general"))
        self.configuration.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(
            self.configuration.default_channels,
            ["Warframe", "TwitchDrops"],
        )
        self.assertTrue(self.configuration.autostart_new_accounts)
        self.assertEqual(self.account.channel_revision, old_revision + 1)
        self.assertTrue(
            MinerCommand.objects.filter(
                account=self.account,
                action=MinerCommand.Action.RESTART,
                actor=self.staff,
            ).exists()
        )
        action = ActionLog.objects.get(action="farm_settings_updated")
        self.assertEqual(action.actor, self.staff)
        self.assertEqual(action.details["default_channels"], ["Warframe", "TwitchDrops"])

        page = self.client.get(reverse("controller:settings_general"))
        self.assertContains(page, "data-channel-editor-root")
        self.assertContains(page, "Ordered default channels")

    def test_general_settings_requires_csrf_for_staff_mutation(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)

        response = client.post(
            reverse("controller:settings_general"),
            {
                "default_channels": "replacement",
                "autostart_new_accounts": "on",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.configuration.refresh_from_db()
        self.assertEqual(self.configuration.default_channels, ["warframe", "twitch"])
        self.assertFalse(self.configuration.autostart_new_accounts)
        self.assertFalse(ActionLog.objects.filter(action="farm_settings_updated").exists())

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
        detail = self.client.get(reverse("controller:preset_detail", args=[preset.pk]))
        self.assertContains(detail, "Edit preset")
        self.assertContains(detail, "Save preset")
        self.assertContains(detail, "data-channel-editor-root")
        self.assertContains(detail, "data-channel-list")
        self.assertEqual(
            self.client.get(reverse("controller:preset_edit", args=[preset.pk])).status_code,
            405,
        )

    def test_preset_page_initial_html_shows_only_assignable_public_usernames(self):
        self.login()
        preset = self.create_preset()
        private_key = "private-ledger-key"
        public_username = "visible_twitch_user"
        twitch_password = "visible-account-secret"
        visible = MinerAccount.objects.create(
            config_key=private_key,
            display_username=public_username,
            is_active=True,
        )
        visible_credential = AccountCredential.objects.create(
            account=visible,
            password_ciphertext=encrypt_text(twitch_password),
        )
        AccountChannelSelection.objects.create(account=visible)
        MinerInstanceState.objects.create(account=visible)
        archived = MinerAccount.objects.create(
            config_key="archived-private-key",
            display_username="archived_public_user",
            is_active=False,
        )
        AccountCredential.objects.create(
            account=archived,
            password_ciphertext=encrypt_text("archived-account-secret"),
        )
        AccountChannelSelection.objects.create(account=archived)
        MinerInstanceState.objects.create(account=archived)
        credentialless = MinerAccount.objects.create(
            config_key="credentialless-private-key",
            display_username="credentialless_public_user",
            is_active=True,
        )
        AccountChannelSelection.objects.create(account=credentialless)
        MinerInstanceState.objects.create(account=credentialless)

        response = self.client.get(
            reverse("controller:preset_detail", args=[preset.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, public_username)
        self.assertContains(
            response,
            reverse("controller:account_info_fragment", args=[visible.pk]),
        )
        self.assertNotContains(response, private_key)
        self.assertNotContains(response, twitch_password)
        self.assertNotContains(response, visible_credential.password_ciphertext)
        self.assertNotContains(response, archived.display_username)
        self.assertNotContains(response, credentialless.display_username)
        self.assertNotContains(response, "Internal account key")

    def test_preset_assignment_updates_both_selected_and_deselected_accounts(self):
        self.login()
        preset = self.create_preset()
        second = MinerAccount.objects.create(
            config_key="second",
            display_username="second_twitch",
            is_active=True,
        )
        AccountCredential.objects.create(
            account=second,
            password_ciphertext=encrypt_text("second-twitch-password"),
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

    def test_account_pages_never_render_stored_credentials(self):
        self.login()
        preset = self.create_preset()
        urls = (
            reverse("controller:account_list"),
            reverse("controller:account_detail", args=[self.account.pk]),
            reverse("controller:account_edit", args=[self.account.pk]),
            reverse("controller:preset_detail", args=[preset.pk]),
        )

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, self.twitch_password)
                self.assertNotContains(response, self.credential.password_ciphertext)
