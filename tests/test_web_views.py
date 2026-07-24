from __future__ import annotations

from datetime import timedelta
import gzip
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from controller import runtime_logs
from controller.crypto import decrypt_text, encrypt_text
from controller.models import (
    AccountChannelSelection,
    AccountCredential,
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
from controller.twitch_lookup import TwitchLookupStatus
from controller.runtime_logs import AccountRunLogWriter


class ApiContractTests(TestCase):
    password = "operator-test-password"

    def setUp(self):
        form_lookup = patch("controller.forms.lookup_twitch_names")
        api_lookup = patch("controller.api.lookup_twitch_names")
        self.form_lookup = form_lookup.start()
        self.api_lookup = api_lookup.start()
        self.addCleanup(form_lookup.stop)
        self.addCleanup(api_lookup.stop)
        exists = lambda names: {name: TwitchLookupStatus.EXISTS for name in names}
        self.form_lookup.side_effect = exists
        self.api_lookup.side_effect = exists

        self.staff = get_user_model().objects.create_user(
            username="night-operator",
            password=self.password,
            is_staff=True,
        )
        self.account = MinerAccount.objects.create(
            config_key="primary",
            display_username="primary_twitch",
            is_active=True,
        )
        self.twitch_password = "never-return-this-password"
        AccountCredential.objects.create(
            account=self.account,
            password_ciphertext=encrypt_text(self.twitch_password),
        )
        AccountChannelSelection.objects.create(account=self.account)
        self.configuration = FarmConfiguration.load()
        self.configuration.default_channels = ["warframe", "twitch"]
        self.configuration.save()
        self.state = MinerInstanceState.objects.create(
            account=self.account,
            desired_state="stopped",
            observed_state="stopped",
        )

    def api(self, name: str, *args) -> str:
        return reverse(f"controller:api:{name}", args=args)

    def login(self):
        self.client.force_login(self.staff)

    def json_request(self, method: str, url: str, payload=None, client=None, **extra):
        client = client or self.client
        return getattr(client, method.lower())(
            url,
            data=json.dumps(payload or {}),
            content_type="application/json",
            **extra,
        )

    def create_preset(self, name="Drops", channels=None):
        preset = ChannelPreset.objects.create(name=name)
        for position, channel in enumerate(channels or ["channel_one", "channel_two"]):
            PresetChannel.objects.create(preset=preset, position=position, name=channel)
        return preset

    def create_run(self):
        run = MinerRun.objects.create(
            account=self.account,
            source_mode="default",
            source_name="farm defaults",
            channels=["warframe", "twitch"],
            configuration_fingerprint="a" * 64,
            channel_revision=self.account.channel_revision,
            pid=4321,
            startup_confirmed_at=timezone.now(),
        )
        self.state.current_run = run
        self.state.advisory_pid = run.pid
        self.state.desired_state = "running"
        self.state.observed_state = "running"
        self.state.last_heartbeat = timezone.now()
        self.state.save()
        return run

    def assert_envelope(self, response, status=200):
        self.assertEqual(response.status_code, status, response.content)
        payload = response.json()
        self.assertEqual(set(payload), {"data", "notices"})
        return payload["data"]

    def assert_error(self, response, code: str, status: int):
        self.assertEqual(response.status_code, status, response.content)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], code)
        self.assertEqual(set(payload["error"]), {"code", "message", "fields"})
        return payload["error"]

    def test_healthz_and_spa_shell_expose_no_runtime_or_secret_state(self):
        health = self.client.get(reverse("controller:healthz"))
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.content, b"ok\n")

        for route in (
            reverse("controller:login"),
            reverse("controller:dashboard"),
            reverse("controller:account_list"),
            reverse("controller:account_detail", args=[self.account.pk]),
            reverse("controller:preset_list"),
            reverse("controller:logs"),
            reverse("controller:settings"),
        ):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="root"')
                self.assertContains(response, "controller/app/app.js")
                self.assertNotContains(response, self.account.config_key)
                self.assertNotContains(response, self.twitch_password)

    @override_settings(ALLOWED_HOSTS=["farm.example.com"], SECURE_SSL_REDIRECT=True)
    def test_healthz_accepts_configured_production_host(self):
        response = self.client.get(
            reverse("controller:healthz"),
            HTTP_HOST="farm.example.com",
            HTTP_X_FORWARDED_PROTO="https",
        )
        self.assertEqual(response.status_code, 200)

    def test_old_html_fragment_action_and_import_routes_are_removed(self):
        for route in (
            "/status/",
            "/actions/start/",
            f"/accounts/{self.account.pk}/edit/",
            f"/accounts/{self.account.pk}/info/",
            "/settings/import/",
            "/logs/tail/",
            "/accounts/",
            "/login/",
        ):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 404)

    def test_session_sets_csrf_cookie_and_reports_auth_state(self):
        anonymous = self.client.get(self.api("session"))
        data = self.assert_envelope(anonymous)
        self.assertFalse(data["authenticated"])
        self.assertIn("csrftoken", anonymous.cookies)

        self.login()
        data = self.assert_envelope(self.client.get(self.api("session")))
        self.assertEqual(data["user"]["username"], self.staff.username)

    def test_login_rejects_non_staff_and_logout_clears_session(self):
        user = get_user_model().objects.create_user(
            username="viewer", password="viewer-password", is_staff=False
        )
        rejected = self.json_request(
            "post",
            self.api("session_login"),
            {"username": user.username, "password": "viewer-password"},
        )
        self.assert_error(rejected, "validation_error", 400)

        accepted = self.json_request(
            "post",
            self.api("session_login"),
            {"username": self.staff.username, "password": self.password},
        )
        self.assertTrue(self.assert_envelope(accepted)["authenticated"])
        logged_out = self.json_request("post", self.api("session_logout"))
        self.assertFalse(self.assert_envelope(logged_out)["authenticated"])

    def test_unauthenticated_api_calls_return_json_401_not_redirects(self):
        urls = (
            self.api("runtime"),
            self.api("accounts"),
            self.api("account_detail", self.account.pk),
            self.api("presets"),
            self.api("settings_general"),
            self.api("logs"),
            self.api("log_runs"),
            self.api("log_run_detail", 1),
            self.api("log_run_download", 1),
            self.api("channel_validate"),
        )
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assert_error(response, "authentication_required", 401)
                self.assertNotIn("Location", response)

    def test_mutations_require_csrf_and_return_json_failure(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        response = self.json_request(
            "post", self.api("runtime_actions"), {"action": "stop"}, client=client
        )
        self.assert_error(response, "csrf_failed", 403)
        self.assertFalse(MinerCommand.objects.exists())

        session = client.get(self.api("session"))
        token = session.cookies["csrftoken"].value
        accepted = self.json_request(
            "post",
            self.api("runtime_actions"),
            {"action": "stop"},
            client=client,
            HTTP_X_CSRFTOKEN=token,
        )
        self.assert_envelope(accepted, 202)

    def test_runtime_contract_includes_live_state_events_and_redacts_secrets(self):
        self.login()
        run = self.create_run()
        incident = MinerIncident.objects.create(
            account=self.account,
            run=run,
            kind="unexpected_exit",
            status="open",
            summary="Miner exited unexpectedly",
            details="safe operator detail",
        )
        command = MinerCommand.objects.create(
            account=self.account,
            action="restart",
            status="failed",
            actor=self.staff,
            error="safe failure",
        )
        ActionLog.objects.create(
            actor=self.staff,
            account=self.account,
            action="inspection",
            message="Reviewed account",
        )
        WorkerLease.objects.create(
            owner_id="worker-1",
            pid=999,
            expires_at=timezone.now() + timedelta(seconds=30),
        )
        self.state.watching_channels = ["twitch"]
        self.state.watching_updated_at = timezone.now()
        self.state.save(update_fields=("watching_channels", "watching_updated_at", "updated_at"))

        response = self.client.get(self.api("runtime"))
        data = self.assert_envelope(response)
        self.assertEqual(data["supervisor"]["status"], "healthy")
        self.assertEqual(data["summary"]["observed_running"], 1)
        self.assertEqual(data["accounts"][0]["source"]["channels"], run.channels)
        self.assertEqual(data["accounts"][0]["watching_channels"], ["twitch"])
        self.assertIsNotNone(data["accounts"][0]["watching_updated_at"])
        self.assertEqual(data["incidents"][0]["id"], incident.pk)
        self.assertEqual(data["command_faults"][0]["id"], command.pk)
        serialized = response.content.decode()
        self.assertNotIn(self.twitch_password, serialized)
        self.assertNotIn(self.credential_ciphertext(), serialized)

    def test_watching_channels_expire_and_aggregate_only_for_assigned_preset(self):
        self.login()
        watched_preset = self.create_preset(
            name="Watched",
            channels=["Alpha", "Beta", "Gamma"],
        )
        unrelated_preset = self.create_preset(
            name="Unrelated",
            channels=["Alpha", "Other"],
        )
        self.account.selection.mode = AccountChannelSelection.Mode.PRESET
        self.account.selection.preset = watched_preset
        self.account.selection.save()
        run = MinerRun.objects.create(
            account=self.account,
            source_mode="preset",
            source_name=watched_preset.name,
            channels=watched_preset.channel_names,
            configuration_fingerprint="b" * 64,
            channel_revision=self.account.channel_revision,
            pid=4322,
            startup_confirmed_at=timezone.now(),
        )
        self.state.current_run = run
        self.state.observed_state = MinerInstanceState.ObservedState.RUNNING
        self.state.watching_channels = ["beta", "ALPHA"]
        self.state.watching_updated_at = timezone.now()
        self.state.save()

        presets = self.assert_envelope(self.client.get(self.api("presets")))["presets"]
        by_name = {preset["name"]: preset for preset in presets}
        self.assertEqual(by_name["Watched"]["watching_channels"], ["Alpha", "Beta"])
        self.assertEqual(by_name["Unrelated"]["watching_channels"], [])

        self.state.watching_updated_at = timezone.now() - timedelta(minutes=6)
        self.state.save(update_fields=("watching_updated_at", "updated_at"))
        runtime = self.assert_envelope(self.client.get(self.api("runtime")))
        self.assertEqual(runtime["accounts"][0]["watching_channels"], [])

    def credential_ciphertext(self):
        return AccountCredential.objects.get(account=self.account).password_ciphertext

    def test_global_lifecycle_command_records_operator_and_coalesces(self):
        self.login()
        first = self.json_request("post", self.api("runtime_actions"), {"action": "start"})
        second = self.json_request("post", self.api("runtime_actions"), {"action": "start"})
        self.assertEqual(self.assert_envelope(first, 202)["queued"], 1)
        self.assert_envelope(second, 202)
        commands = MinerCommand.objects.filter(account=self.account, action="start")
        self.assertEqual(commands.count(), 1)
        self.assertEqual(commands.get().actor, self.staff)
        self.state.refresh_from_db()
        self.assertEqual(self.state.desired_state, "running")

    def test_accounts_read_create_update_and_telemetry_contracts(self):
        self.login()
        listing = self.assert_envelope(self.client.get(self.api("accounts")))
        self.assertEqual(listing["active_count"], 1)
        self.assertNotIn("password_ciphertext", json.dumps(listing))
        self.assertNotIn(self.credential_ciphertext(), json.dumps(listing))

        created_response = self.json_request(
            "post",
            self.api("accounts"),
            {
                "config_key": "secondary",
                "username": "secondary_user",
                "password": "secondary-secret",
                "mode": "custom",
                "channels": ["alpha", "beta", "ALPHA"],
                "start_after_save": False,
            },
        )
        created = self.assert_envelope(created_response, 201)
        account = MinerAccount.objects.get(pk=created["id"])
        self.assertEqual(decrypt_text(account.credential.password_ciphertext), "secondary-secret")
        self.assertEqual(
            list(account.custom_channels.order_by("position").values_list("name", flat=True)),
            ["alpha", "beta"],
        )
        self.assertNotIn("secondary-secret", created_response.content.decode())

        updated = self.json_request(
            "patch",
            self.api("account_detail", account.pk),
            {"username": "secondary_renamed", "password": ""},
        )
        self.assertEqual(self.assert_envelope(updated)["username"], "secondary_renamed")
        telemetry = self.assert_envelope(
            self.client.get(self.api("account_telemetry", account.pk))
        )
        self.assertEqual(telemetry["account"]["id"], account.pk)

    def test_passwordless_tv_account_and_reconnect_api_are_redacted(self):
        self.login()
        response = self.json_request(
            "post",
            self.api("accounts"),
            {
                "config_key": "tv-account",
                "username": "tv_viewer",
                "mode": "default",
                "channels": [],
                "start_after_save": False,
            },
        )
        created = self.assert_envelope(response, 201)
        account = MinerAccount.objects.get(pk=created["id"])
        self.assertEqual(created["authentication"]["method"], "twitch_tv")
        self.assertEqual(created["authentication"]["status"], "unlinked")
        self.assertEqual(account.credential.password_ciphertext, "")

        reconnect = self.client.post(
            self.api("account_tv_authentication", account.pk),
            data="{}",
            content_type="application/json",
        )
        data = self.assert_envelope(reconnect, 202)
        self.assertEqual(data["command"]["action"], "authenticate")
        serialized = reconnect.content.decode()
        self.assertNotIn("ciphertext", serialized)
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("device_code", serialized)

    def test_account_validation_envelope_and_sensitive_failure_redaction(self):
        self.login()
        response = self.json_request(
            "post",
            self.api("accounts"),
            {
                "config_key": "primary",
                "username": "bad user",
                "password": "do-not-echo-this",
                "mode": "custom",
                "channels": [],
            },
        )
        error = self.assert_error(response, "validation_error", 400)
        self.assertIn("config_key", error["fields"])
        self.assertIn("channels", error["fields"])
        self.assertNotIn("custom_channels", error["fields"])
        self.assertNotIn("do-not-echo-this", response.content.decode())

        missing_preset = self.json_request(
            "post",
            self.api("accounts"),
            {
                "config_key": "preset-missing",
                "username": "preset_user",
                "password": "do-not-echo-this",
                "mode": "preset",
                "preset_id": None,
                "channels": [],
            },
        )
        preset_error = self.assert_error(missing_preset, "validation_error", 400)
        self.assertIn("preset_id", preset_error["fields"])
        self.assertNotIn("preset", preset_error["fields"])

    def test_inactive_account_is_read_only_but_stop_remains_available(self):
        self.login()
        self.account.is_active = False
        self.account.save(update_fields=("is_active", "updated_at"))
        update = self.json_request(
            "patch",
            self.api("account_detail", self.account.pk),
            {"username": "changed", "password": ""},
        )
        self.assert_error(update, "inactive_account", 409)
        restart = self.json_request(
            "post", self.api("account_actions", self.account.pk), {"action": "restart"}
        )
        self.assert_error(restart, "validation_error", 400)
        stop = self.json_request(
            "post", self.api("account_actions", self.account.pk), {"action": "stop"}
        )
        self.assert_envelope(stop, 202)

    def test_channel_source_preserves_order_and_queues_restart(self):
        self.login()
        self.state.desired_state = "running"
        self.state.save(update_fields=("desired_state", "updated_at"))
        response = self.json_request(
            "put",
            self.api("account_channel_source", self.account.pk),
            {"mode": "custom", "channels": ["beta", "alpha", "BETA"]},
        )
        data = self.assert_envelope(response)
        self.assertEqual(data["planned_source"]["channels"], ["beta", "alpha"])
        self.assertTrue(
            MinerCommand.objects.filter(account=self.account, action="restart").exists()
        )
        action = ActionLog.objects.filter(action="channel_selection_changed").get()
        self.assertEqual(action.actor, self.staff)

    def test_channel_validation_distinguishes_exists_missing_and_unverified(self):
        self.login()
        for status in TwitchLookupStatus:
            with self.subTest(status=status):
                self.api_lookup.return_value = {"channel_name": status}
                self.api_lookup.side_effect = None
                response = self.client.get(
                    self.api("channel_validate"), {"name": "channel_name"}
                )
                data = self.assert_envelope(response)
                self.assertEqual(data, {"name": "channel_name", "status": status.value})

    def test_preset_crud_assignment_and_delete_protection(self):
        self.login()
        created_response = self.json_request(
            "post",
            self.api("presets"),
            {"name": "Night", "channels": ["one", "two", "ONE"]},
        )
        created = self.assert_envelope(created_response, 201)
        preset = ChannelPreset.objects.get(pk=created["id"])
        self.assertEqual(preset.channel_names, ["one", "two"])

        invalid_assignment = self.json_request(
            "put",
            self.api("preset_assignments", preset.pk),
            {"account_ids": [999999]},
        )
        assignment_error = self.assert_error(
            invalid_assignment, "validation_error", 400
        )
        self.assertIn("account_ids", assignment_error["fields"])
        self.assertNotIn("accounts", assignment_error["fields"])

        assignment = self.json_request(
            "put",
            self.api("preset_assignments", preset.pk),
            {"account_ids": [self.account.pk]},
        )
        detail = self.assert_envelope(assignment)
        self.assertEqual(detail["assigned_account_ids"], [self.account.pk])
        self.account.selection.refresh_from_db()
        self.assertEqual(self.account.selection.preset, preset)

        blocked = self.client.delete(
            self.api("preset_detail", preset.pk),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assert_error(blocked, "validation_error", 400)

        self.json_request(
            "put", self.api("preset_assignments", preset.pk), {"account_ids": []}
        )
        deleted = self.client.delete(
            self.api("preset_detail", preset.pk),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertTrue(self.assert_envelope(deleted)["deleted"])

    def test_preset_update_restarts_desired_running_assignments(self):
        self.login()
        preset = self.create_preset()
        self.account.selection.mode = "preset"
        self.account.selection.preset = preset
        self.account.selection.save()
        self.state.desired_state = "running"
        self.state.save(update_fields=("desired_state", "updated_at"))
        response = self.json_request(
            "put",
            self.api("preset_detail", preset.pk),
            {"name": "Drops", "channels": ["new_one", "new_two"]},
        )
        self.assert_envelope(response)
        self.assertTrue(
            MinerCommand.objects.filter(account=self.account, action="restart").exists()
        )

    def test_general_settings_validate_and_restart_default_accounts(self):
        self.login()
        self.state.desired_state = "running"
        self.state.save(update_fields=("desired_state", "updated_at"))
        response = self.json_request(
            "put",
            self.api("settings_general"),
            {"default_channels": ["new_default", "other"], "autostart_new_accounts": True},
        )
        data = self.assert_envelope(response)
        self.assertEqual(data["default_channels"], ["new_default", "other"])
        self.assertTrue(data["autostart_new_accounts"])
        self.assertTrue(
            MinerCommand.objects.filter(account=self.account, action="restart").exists()
        )
        self.assertEqual(
            ActionLog.objects.filter(action="farm_settings_updated").get().actor,
            self.staff,
        )

    def test_logs_are_bounded_and_never_cache(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "twitch-farm.log"
            path.write_text("\n".join(f"line-{index}" for index in range(450)))
            with override_settings(TWITCH_FARM_LOG_FILE=path):
                response = self.client.get(self.api("logs"))
        data = self.assert_envelope(response)
        self.assertEqual(data["line_count"], 400)
        self.assertEqual(data["lines"][0], "line-50")
        self.assertEqual(data["source"]["kind"], "combined")
        self.assertIsNotNone(data["cursor"])
        self.assertFalse(data["reset"])
        self.assertIn("no-cache", response.headers["Cache-Control"])

    def test_account_live_history_detail_and_gzip_download(self):
        self.login()
        run = self.create_run()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "twitch-farm.log"
            with override_settings(TWITCH_FARM_LOG_FILE=path):
                writer = AccountRunLogWriter(
                    account_id=self.account.pk,
                    run_id=run.pk,
                    account_key=self.account.config_key,
                )
                writer.lifecycle("process_started", pid=4321)
                writer.write("account-specific-line")

                live = self.client.get(
                    self.api("logs"),
                    {"account_id": self.account.pk},
                )
                live_data = self.assert_envelope(live)
                self.assertEqual(live_data["source"]["account_id"], self.account.pk)
                self.assertEqual(live_data["run_id"], run.pk)
                self.assertTrue(
                    any("account-specific-line" in line for line in live_data["lines"])
                )

                writer.finalize("run_finished", reason="admin_stop")
                ended_at = timezone.now()
                MinerRun.objects.filter(pk=run.pk).update(
                    ended_at=ended_at,
                    exit_code=0,
                    stop_reason=MinerRun.StopReason.ADMIN_STOP,
                )
                self.state.current_run = None
                self.state.advisory_pid = None
                self.state.observed_state = MinerInstanceState.ObservedState.STOPPED
                self.state.save()
                self.account.is_active = False
                self.account.save(update_fields=("is_active", "updated_at"))

                history = self.client.get(
                    self.api("log_runs"),
                    {"account_id": self.account.pk},
                )
                history_data = self.assert_envelope(history)
                self.assertEqual(history_data["runs"][0]["run_id"], run.pk)
                self.assertTrue(history_data["runs"][0]["downloadable"])
                self.assertFalse(history_data["runs"][0]["account"]["is_active"])

                detail = self.client.get(self.api("log_run_detail", run.pk))
                detail_data = self.assert_envelope(detail)
                self.assertEqual(detail_data["run"]["stop_reason"], "admin_stop")
                self.assertTrue(
                    any("account-specific-line" in line for line in detail_data["lines"])
                )

                download = self.client.get(self.api("log_run_download", run.pk))
                compressed = b"".join(download.streaming_content)

        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["Content-Type"], "application/gzip")
        self.assertIn(f"run-{run.pk}.log.gz", download["Content-Disposition"])
        self.assertIn("account-specific-line", gzip.decompress(compressed).decode("utf-8"))
        self.assertIn("no-cache", history.headers["Cache-Control"])
        self.assertIn("no-cache", detail.headers["Cache-Control"])
        self.assertIn("no-cache", download.headers["Cache-Control"])

    def test_log_api_rejects_invalid_cursor_and_active_download(self):
        self.login()
        invalid = self.client.get(self.api("logs"), {"cursor": "not-a-signed-cursor"})
        self.assert_error(invalid, "log_unavailable", 409)

        run = self.create_run()
        active = self.client.get(self.api("log_run_download", run.pk))
        self.assert_error(active, "log_still_active", 409)

    def test_log_api_exposes_pending_plaintext_and_blocks_its_download(self):
        self.login()
        run = self.create_run()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "twitch-farm.log"
            with override_settings(TWITCH_FARM_LOG_FILE=path):
                writer = AccountRunLogWriter(
                    account_id=self.account.pk,
                    run_id=run.pk,
                    account_key=self.account.config_key,
                )
                writer.write("readable pending line")
                with patch(
                    "controller.runtime_logs._compress_plain_part",
                    side_effect=OSError("simulated compression failure"),
                ):
                    writer.finalize("final_exit")
                MinerRun.objects.filter(pk=run.pk).update(
                    ended_at=timezone.now(),
                    stop_reason=MinerRun.StopReason.ADMIN_STOP,
                )

                detail = self.client.get(self.api("log_run_detail", run.pk))
                detail_data = self.assert_envelope(detail)
                download = self.client.get(self.api("log_run_download", run.pk))

        self.assertEqual(detail_data["run"]["archive_state"], "compression_pending")
        self.assertTrue(any("readable pending line" in line for line in detail_data["lines"]))
        self.assert_error(download, "log_unavailable", 409)

    def test_log_download_returns_redacted_error_when_retention_wins_open_race(self):
        self.login()
        run = self.create_run()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "twitch-farm.log"
            with override_settings(TWITCH_FARM_LOG_FILE=path):
                writer = AccountRunLogWriter(
                    account_id=self.account.pk,
                    run_id=run.pk,
                    account_key=self.account.config_key,
                )
                writer.write("removed during download setup")
                writer.finalize("final_exit")
                MinerRun.objects.filter(pk=run.pk).update(
                    ended_at=timezone.now(),
                    stop_reason=MinerRun.StopReason.ADMIN_STOP,
                )
                original_parts = runtime_logs._run_parts
                calls = 0

                def prune_before_download_open(account_id, run_id):
                    nonlocal calls
                    calls += 1
                    parts = original_parts(account_id, run_id)
                    if calls == 2:
                        parts[0].path.unlink()
                    return parts

                with patch(
                    "controller.runtime_logs._run_parts",
                    side_effect=prune_before_download_open,
                ):
                    download = self.client.get(self.api("log_run_download", run.pk))

        self.assert_error(download, "log_unavailable", 409)

    def test_truncated_download_filename_labels_the_retained_suffix(self):
        self.login()
        run = self.create_run()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "twitch-farm.log"
            with override_settings(
                TWITCH_FARM_LOG_FILE=path,
                TWITCH_FARM_ACCOUNT_LOG_PART_BYTES=180,
                TWITCH_FARM_ACCOUNT_LOG_ARCHIVE_BYTES=240,
            ):
                writer = AccountRunLogWriter(
                    account_id=self.account.pk,
                    run_id=run.pk,
                    account_key=self.account.config_key,
                )
                for index in range(40):
                    writer.write(f"retention-{index:03d}-{index * 7919}-abcdefghijklmnopqrstuvwxyz")
                writer.finalize("final_exit")
                MinerRun.objects.filter(pk=run.pk).update(
                    ended_at=timezone.now(),
                    stop_reason=MinerRun.StopReason.ADMIN_STOP,
                )

                download = self.client.get(self.api("log_run_download", run.pk))
                compressed = b"".join(download.streaming_content)

        self.assertEqual(download.status_code, 200)
        self.assertIn("-truncated.log.gz", download["Content-Disposition"])
        self.assertTrue(gzip.decompress(compressed))

    def test_api_methods_return_json_405_and_allow_header(self):
        self.login()
        response = self.client.put(
            self.api("runtime"), data=json.dumps({}), content_type="application/json"
        )
        self.assert_error(response, "method_not_allowed", 405)
        self.assertEqual(response.headers["Allow"], "GET")
