"""Durable controller state for the Twitch farm.

Runtime configuration is database-backed.  Sensitive values are stored only as
application-encrypted ciphertext and are never copied into launch snapshots or
audit records.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


def validate_channel_snapshot(value: Any) -> None:
    """Validate the immutable list of channels attached to a miner run."""

    if not isinstance(value, list) or not value:
        raise ValidationError("A miner run must contain at least one channel.")
    if any(not isinstance(channel, str) or not channel.strip() for channel in value):
        raise ValidationError("Every channel in a miner run must be a non-empty string.")


class MinerAccount(models.Model):
    """A UI-managed Twitch account with an immutable internal key."""

    config_key = models.CharField(max_length=150, unique=True)
    display_username = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True, db_index=True)
    channel_revision = models.PositiveBigIntegerField(default=1)
    configuration_fingerprint = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("config_key",)

    def __str__(self) -> str:
        return self.display_username or self.config_key

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk:
            original_key = type(self).objects.filter(pk=self.pk).values_list(
                "config_key", flat=True
            ).first()
            if original_key is not None and original_key != self.config_key:
                raise ValidationError("An account's internal key is immutable.")
        super().save(*args, **kwargs)

    @property
    def has_credentials(self) -> bool:
        try:
            self.credential
        except AccountCredential.DoesNotExist:
            return False
        return True


class AccountCredential(models.Model):
    """Encrypted launch credential for one account."""

    class AuthMethod(models.TextChoices):
        TWITCH_TV = "twitch_tv", "Twitch TV device login"
        LEGACY_PASSWORD = "legacy_password", "Legacy password"

    account = models.OneToOneField(
        MinerAccount,
        on_delete=models.CASCADE,
        related_name="credential",
    )
    auth_method = models.CharField(
        max_length=24,
        choices=AuthMethod.choices,
        default=AuthMethod.LEGACY_PASSWORD,
    )
    password_ciphertext = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Credential for {self.account.config_key}"


class AccountSessionSeed(models.Model):
    """Encrypted, normalized legacy cookies awaiting worker-only placement."""

    account = models.OneToOneField(
        MinerAccount,
        on_delete=models.CASCADE,
        related_name="session_seed",
    )
    payload_ciphertext = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Session seed for {self.account.config_key}"


class FarmConfiguration(models.Model):
    """Singleton UI-managed global launch defaults."""

    default_channels = models.JSONField(default=list, blank=True)
    autostart_new_accounts = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls) -> "FarmConfiguration":
        configuration, _ = cls.objects.get_or_create(pk=1)
        return configuration

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return "Farm configuration"


class LegacyImportDraft(models.Model):
    """Short-lived encrypted payload reviewed before a legacy import commit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="legacy_import_drafts",
    )
    payload_ciphertext = models.TextField()
    preview = models.JSONField(default=dict, blank=True)
    source_digest = models.CharField(max_length=64)
    baseline_digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    def __str__(self) -> str:
        return f"Legacy import draft {self.pk}"


class ChannelPreset(models.Model):
    """A named, ordered set of channels assignable to accounts."""

    name = models.CharField(max_length=150, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    @property
    def channel_names(self) -> list[str]:
        return list(self.channels.order_by("position").values_list("name", flat=True))


class PresetChannel(models.Model):
    preset = models.ForeignKey(
        ChannelPreset,
        on_delete=models.CASCADE,
        related_name="channels",
    )
    name = models.CharField(max_length=100)
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ("position", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("preset", "position"),
                name="controller_unique_preset_channel_position",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class AccountChannelSelection(models.Model):
    class Mode(models.TextChoices):
        DEFAULT = "default", "Default channels"
        CUSTOM = "custom", "Custom channels"
        PRESET = "preset", "Preset"

    account = models.OneToOneField(
        MinerAccount,
        on_delete=models.CASCADE,
        related_name="selection",
    )
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.DEFAULT)
    preset = models.ForeignKey(
        ChannelPreset,
        on_delete=models.PROTECT,
        related_name="account_selections",
        null=True,
        blank=True,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(mode="preset", preset__isnull=False)
                    | Q(mode__in=("default", "custom"), preset__isnull=True)
                ),
                name="controller_selection_mode_matches_preset",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.account.config_key}: {self.mode}"


class AccountCustomChannel(models.Model):
    account = models.ForeignKey(
        MinerAccount,
        on_delete=models.CASCADE,
        related_name="custom_channels",
    )
    name = models.CharField(max_length=100)
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ("position", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("account", "position"),
                name="controller_unique_custom_channel_position",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class MinerRun(models.Model):
    """A launch record whose channel specification cannot be rewritten."""

    class StopReason(models.TextChoices):
        ADMIN_STOP = "admin_stop", "Admin stop"
        CONFIG_RESTART = "config_restart", "Configuration restart"
        ADMIN_RESTART = "admin_restart", "Admin restart"
        SUPERVISOR_SHUTDOWN = "supervisor_shutdown", "Supervisor shutdown"
        UNEXPECTED_EXIT = "unexpected_exit", "Unexpected exit"
        START_FAILED = "start_failed", "Start failed"
        AUTHENTICATION_FAILED = "authentication_failed", "Authentication failed"
        AUTHENTICATION_RESET = "authentication_reset", "Authentication reset"

    IMMUTABLE_FIELDS = (
        "account_id",
        "source_mode",
        "source_name",
        "channels",
        "configuration_fingerprint",
        "channel_revision",
        "auth_method",
        "reset_session",
    )

    account = models.ForeignKey(MinerAccount, on_delete=models.PROTECT, related_name="runs")
    source_mode = models.CharField(max_length=16, choices=AccountChannelSelection.Mode.choices)
    source_name = models.CharField(max_length=150, blank=True)
    channels = models.JSONField(validators=(validate_channel_snapshot,))
    configuration_fingerprint = models.CharField(max_length=64)
    channel_revision = models.PositiveBigIntegerField()
    auth_method = models.CharField(
        max_length=24,
        choices=AccountCredential.AuthMethod.choices,
        default=AccountCredential.AuthMethod.LEGACY_PASSWORD,
    )
    reset_session = models.BooleanField(default=False)
    pid = models.PositiveIntegerField(null=True, blank=True)
    worker_id = models.CharField(max_length=200, blank=True)
    startup_confirmed_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True, db_index=True)
    exit_code = models.IntegerField(null=True, blank=True)
    exit_signal = models.IntegerField(null=True, blank=True)
    stop_reason = models.CharField(max_length=32, choices=StopReason.choices, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-started_at", "-id")
        indexes = [
            models.Index(fields=("account", "ended_at"), name="ctrl_run_account_ended_idx"),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS).first()
            if original and any(original[field] != getattr(self, field) for field in self.IMMUTABLE_FIELDS):
                raise ValidationError("A miner run's launch specification is immutable.")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Run {self.pk or 'new'} for {self.account.config_key}"


class MinerInstanceState(models.Model):
    class AuthenticationStatus(models.TextChoices):
        UNLINKED = "unlinked", "Unlinked"
        PENDING = "pending", "Pending"
        AUTHENTICATED = "authenticated", "Authenticated"
        REAUTH_REQUIRED = "reauth_required", "Reauthentication required"

    class DesiredState(models.TextChoices):
        STOPPED = "stopped", "Stopped"
        RUNNING = "running", "Running"

    class ObservedState(models.TextChoices):
        STARTING = "starting", "Starting"
        RUNNING = "running", "Running"
        STOPPING = "stopping", "Stopping"
        STOPPED = "stopped", "Stopped"
        RESTARTING = "restarting", "Restarting"
        DEGRADED = "degraded", "Degraded"
        UNKNOWN = "unknown", "Unknown"

    account = models.OneToOneField(
        MinerAccount,
        on_delete=models.CASCADE,
        related_name="runtime_state",
    )
    desired_state = models.CharField(
        max_length=16,
        choices=DesiredState.choices,
        default=DesiredState.STOPPED,
        db_index=True,
    )
    observed_state = models.CharField(
        max_length=16,
        choices=ObservedState.choices,
        default=ObservedState.UNKNOWN,
        db_index=True,
    )
    current_run = models.OneToOneField(
        MinerRun,
        on_delete=models.SET_NULL,
        related_name="current_for_state",
        null=True,
        blank=True,
    )
    advisory_pid = models.PositiveIntegerField(null=True, blank=True)
    worker_id = models.CharField(max_length=200, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    stable_since = models.DateTimeField(null=True, blank=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    watching_channels = models.JSONField(default=list, blank=True)
    online_channels = models.JSONField(default=list, blank=True)
    watching_updated_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    authentication_status = models.CharField(
        max_length=24,
        choices=AuthenticationStatus.choices,
        default=AuthenticationStatus.UNLINKED,
        db_index=True,
    )
    authentication_uri = models.URLField(blank=True)
    authentication_code = models.CharField(max_length=64, blank=True)
    authentication_expires_at = models.DateTimeField(null=True, blank=True)
    authentication_error = models.TextField(blank=True)
    authentication_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("account__config_key",)

    def save(self, *args: Any, **kwargs: Any) -> None:
        if (
            self.current_run_id is None
            or self.observed_state
            not in (self.ObservedState.STARTING, self.ObservedState.RUNNING)
        ):
            self.watching_channels = []
            self.online_channels = []
            self.watching_updated_at = None
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = tuple(
                    dict.fromkeys(
                        (
                            *update_fields,
                            "watching_channels",
                            "online_channels",
                            "watching_updated_at",
                        )
                    )
                )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.account.config_key}: {self.desired_state}/{self.observed_state}"


class MinerCommand(models.Model):
    class Action(models.TextChoices):
        START = "start", "Start"
        STOP = "stop", "Stop"
        RESTART = "restart", "Restart"
        AUTHENTICATE = "authenticate", "Authenticate"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        LEASED = "leased", "Leased"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    account = models.ForeignKey(MinerAccount, on_delete=models.CASCADE, related_name="commands")
    action = models.CharField(max_length=16, choices=Action.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="miner_commands",
        null=True,
        blank=True,
    )
    reason = models.CharField(max_length=255, blank=True)
    lease_owner = models.CharField(max_length=200, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    leased_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [
            models.Index(fields=("status", "created_at"), name="ctrl_cmd_status_created_idx"),
            models.Index(fields=("account", "status"), name="ctrl_cmd_account_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.account.config_key} ({self.status})"


class MinerIncident(models.Model):
    class Kind(models.TextChoices):
        UNEXPECTED_EXIT = "unexpected_exit", "Unexpected miner exit"
        UNCLEAN_SUPERVISOR = "unclean_supervisor", "Unclean supervisor shutdown"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RECOVERED = "recovered", "Recovered"

    account = models.ForeignKey(
        MinerAccount,
        on_delete=models.SET_NULL,
        related_name="incidents",
        null=True,
        blank=True,
    )
    run = models.ForeignKey(
        MinerRun,
        on_delete=models.SET_NULL,
        related_name="incidents",
        null=True,
        blank=True,
    )
    kind = models.CharField(max_length=32, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    summary = models.CharField(max_length=255)
    details = models.TextField(blank=True)
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    recovered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-opened_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("account",),
                condition=Q(status="open", account__isnull=False),
                name="controller_one_open_incident_per_account",
            ),
        ]

    def __str__(self) -> str:
        subject = self.account.config_key if self.account_id else "supervisor"
        return f"{subject}: {self.summary}"


class RestartAttempt(models.Model):
    class Outcome(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        STARTED = "started", "Started"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    incident = models.ForeignKey(
        MinerIncident,
        on_delete=models.CASCADE,
        related_name="restart_attempts",
    )
    run = models.ForeignKey(
        MinerRun,
        on_delete=models.SET_NULL,
        related_name="restart_attempts",
        null=True,
        blank=True,
    )
    attempt_number = models.PositiveIntegerField()
    scheduled_at = models.DateTimeField()
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SCHEDULED)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("attempt_number", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("incident", "attempt_number"),
                name="controller_unique_restart_attempt_number",
            ),
        ]

    def __str__(self) -> str:
        return f"Incident {self.incident_id} attempt {self.attempt_number}"


class ActionLog(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="twitch_farm_actions",
        null=True,
        blank=True,
    )
    account = models.ForeignKey(
        MinerAccount,
        on_delete=models.SET_NULL,
        related_name="action_logs",
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=100, db_index=True)
    message = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-id")

    def __str__(self) -> str:
        return self.message or self.action


class WorkerLease(models.Model):
    """Database-visible heartbeat for the one process-owning supervisor."""

    name = models.CharField(max_length=100, unique=True, default="miner-supervisor")
    owner_id = models.CharField(max_length=200)
    pid = models.PositiveIntegerField(null=True, blank=True)
    acquired_at = models.DateTimeField(default=timezone.now)
    heartbeat_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.name}: {self.owner_id}"
