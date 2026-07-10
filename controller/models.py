"""Durable controller state for the Twitch farm.

Secrets deliberately do not belong in these models.  Twitch credentials stay in
``config.yaml`` and are loaded by the process-owning worker at launch time.
"""

from __future__ import annotations

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
    """A non-secret mirror of an account defined in ``config.yaml``."""

    config_key = models.CharField(max_length=150, unique=True)
    display_username = models.CharField(max_length=150)
    is_configured = models.BooleanField(default=True, db_index=True)
    channel_revision = models.PositiveBigIntegerField(default=1)
    configuration_fingerprint = models.CharField(max_length=64, blank=True)
    config_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("config_key",)

    def __str__(self) -> str:
        return self.display_username or self.config_key


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

    IMMUTABLE_FIELDS = (
        "account_id",
        "source_mode",
        "source_name",
        "channels",
        "configuration_fingerprint",
        "channel_revision",
    )

    account = models.ForeignKey(MinerAccount, on_delete=models.PROTECT, related_name="runs")
    source_mode = models.CharField(max_length=16, choices=AccountChannelSelection.Mode.choices)
    source_name = models.CharField(max_length=150, blank=True)
    channels = models.JSONField(validators=(validate_channel_snapshot,))
    configuration_fingerprint = models.CharField(max_length=64)
    channel_revision = models.PositiveBigIntegerField()
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
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("account__config_key",)

    def __str__(self) -> str:
        return f"{self.account.config_key}: {self.desired_state}/{self.observed_state}"


class MinerCommand(models.Model):
    class Action(models.TextChoices):
        START = "start", "Start"
        STOP = "stop", "Stop"
        RESTART = "restart", "Restart"

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
