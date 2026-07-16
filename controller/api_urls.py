"""Versioned JSON routes for the React control room."""

from django.urls import path

from . import api


app_name = "api"

urlpatterns = [
    path("session", api.session, name="session"),
    path("session/login", api.session_login, name="session_login"),
    path("session/logout", api.session_logout, name="session_logout"),
    path("runtime", api.runtime, name="runtime"),
    path("runtime/actions", api.runtime_actions, name="runtime_actions"),
    path("accounts", api.accounts, name="accounts"),
    path("accounts/<int:pk>", api.account_detail, name="account_detail"),
    path("accounts/<int:pk>/telemetry", api.account_telemetry, name="account_telemetry"),
    path("accounts/<int:pk>/channel-source", api.account_channel_source, name="account_channel_source"),
    path("accounts/<int:pk>/actions", api.account_actions, name="account_actions"),
    path("presets", api.presets, name="presets"),
    path("presets/<int:pk>", api.preset_detail, name="preset_detail"),
    path("presets/<int:pk>/assignments", api.preset_assignments, name="preset_assignments"),
    path("settings/general", api.settings_general, name="settings_general"),
    path("settings/imports", api.settings_imports, name="settings_imports"),
    path("settings/imports/<uuid:draft_id>/confirm", api.settings_import_confirm, name="settings_import_confirm"),
    path("settings/imports/<uuid:draft_id>", api.settings_import_delete, name="settings_import_delete"),
    path("logs", api.logs, name="logs"),
    path("channels/validate", api.channel_validate, name="channel_validate"),
]
