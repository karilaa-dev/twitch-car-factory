"""Read-friendly Django admin registrations for controller state."""

from django.contrib import admin

from .models import (
    AccountChannelSelection,
    ActionLog,
    ChannelPreset,
    MinerAccount,
    MinerCommand,
    MinerIncident,
    MinerInstanceState,
    MinerRun,
    RestartAttempt,
    WorkerLease,
)


class ReadOnlyControllerAdmin(admin.ModelAdmin):
    """Keep Django admin diagnostic; mutations must use controller services."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ChannelPreset)
class ChannelPresetAdmin(ReadOnlyControllerAdmin):
    list_display = ("name", "updated_at")
    search_fields = ("name", "channels__name")


@admin.register(MinerAccount)
class MinerAccountAdmin(ReadOnlyControllerAdmin):
    list_display = (
        "config_key",
        "display_username",
        "is_configured",
        "channel_revision",
        "config_synced_at",
    )
    list_filter = ("is_configured",)
    search_fields = ("config_key", "display_username")
    readonly_fields = ("configuration_fingerprint", "config_synced_at", "created_at", "updated_at")


@admin.register(AccountChannelSelection)
class AccountChannelSelectionAdmin(ReadOnlyControllerAdmin):
    list_display = ("account", "mode", "preset", "updated_at")
    list_filter = ("mode",)
    autocomplete_fields = ("account", "preset")


@admin.register(MinerInstanceState)
class MinerInstanceStateAdmin(ReadOnlyControllerAdmin):
    list_display = (
        "account",
        "desired_state",
        "observed_state",
        "advisory_pid",
        "retry_count",
        "last_heartbeat",
    )
    list_filter = ("desired_state", "observed_state")
    readonly_fields = ("created_at", "updated_at")


@admin.register(MinerCommand)
class MinerCommandAdmin(ReadOnlyControllerAdmin):
    list_display = ("account", "action", "status", "actor", "attempts", "created_at")
    list_filter = ("action", "status")
    search_fields = ("account__config_key", "reason", "error")
    readonly_fields = ("created_at", "updated_at", "leased_at", "completed_at")


@admin.register(MinerRun)
class MinerRunAdmin(ReadOnlyControllerAdmin):
    list_display = (
        "id",
        "account",
        "source_mode",
        "pid",
        "startup_confirmed_at",
        "started_at",
        "ended_at",
        "stop_reason",
    )
    list_filter = ("source_mode", "stop_reason")
    search_fields = ("account__config_key", "source_name", "configuration_fingerprint")
    readonly_fields = (
        "account",
        "source_mode",
        "source_name",
        "channels",
        "configuration_fingerprint",
        "channel_revision",
        "created_at",
    )


class RestartAttemptInline(admin.TabularInline):
    model = RestartAttempt
    extra = 0
    readonly_fields = ("created_at",)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MinerIncident)
class MinerIncidentAdmin(ReadOnlyControllerAdmin):
    list_display = ("summary", "account", "kind", "status", "opened_at", "recovered_at")
    list_filter = ("kind", "status")
    search_fields = ("summary", "details", "account__config_key")
    inlines = (RestartAttemptInline,)


@admin.register(ActionLog)
class ActionLogAdmin(ReadOnlyControllerAdmin):
    list_display = ("created_at", "action", "account", "actor", "message")
    list_filter = ("action",)
    search_fields = ("message", "account__config_key", "actor__username")
    readonly_fields = ("actor", "account", "action", "message", "details", "created_at")

@admin.register(WorkerLease)
class WorkerLeaseAdmin(ReadOnlyControllerAdmin):
    list_display = ("name", "owner_id", "pid", "heartbeat_at", "expires_at")
    readonly_fields = ("name", "owner_id", "pid", "acquired_at", "heartbeat_at", "expires_at")
