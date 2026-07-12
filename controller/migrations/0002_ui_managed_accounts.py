import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def stop_legacy_accounts(apps, schema_editor):
    MinerAccount = apps.get_model("controller", "MinerAccount")
    MinerInstanceState = apps.get_model("controller", "MinerInstanceState")
    FarmConfiguration = apps.get_model("controller", "FarmConfiguration")

    MinerAccount.objects.update(is_active=False, configuration_fingerprint="")
    MinerInstanceState.objects.update(
        desired_state="stopped",
        observed_state="unknown",
        current_run=None,
        advisory_pid=None,
        worker_id="",
        next_retry_at=None,
        stable_since=None,
    )
    FarmConfiguration.objects.get_or_create(pk=1)


class Migration(migrations.Migration):

    dependencies = [
        ("controller", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameField(
            model_name="mineraccount",
            old_name="is_configured",
            new_name="is_active",
        ),
        migrations.RemoveField(
            model_name="mineraccount",
            name="config_synced_at",
        ),
        migrations.CreateModel(
            name="FarmConfiguration",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("default_channels", models.JSONField(blank=True, default=list)),
                ("autostart_new_accounts", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="AccountCredential",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("password_ciphertext", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="credential",
                        to="controller.mineraccount",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="AccountSessionSeed",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("payload_ciphertext", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "account",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="session_seed",
                        to="controller.mineraccount",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="LegacyImportDraft",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("payload_ciphertext", models.TextField()),
                ("preview", models.JSONField(blank=True, default=dict)),
                ("source_digest", models.CharField(max_length=64)),
                ("baseline_digest", models.CharField(max_length=64)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="legacy_import_drafts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.RunPython(stop_legacy_accounts, migrations.RunPython.noop),
    ]
