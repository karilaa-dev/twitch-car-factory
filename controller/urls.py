"""Routes for the staff-only web control room."""

from django.urls import path

from . import views


app_name = "controller"

urlpatterns = [
    path("healthz/", views.healthz, name="healthz"),
    path("login/", views.StaffLoginView.as_view(), name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("status/", views.status_fragment, name="status_fragment"),
    path("actions/<str:action>/", views.global_action, name="global_action"),
    path("logs/", views.bot_logs, name="bot_logs"),
    path("logs/tail/", views.bot_log_tail, name="bot_log_tail"),
    path("accounts/", views.account_list, name="account_list"),
    path("accounts/new/", views.account_create, name="account_create"),
    path("accounts/<int:pk>/", views.account_detail, name="account_detail"),
    path("accounts/<int:pk>/edit/", views.account_edit, name="account_edit"),
    path(
        "accounts/<int:pk>/info/",
        views.account_info_fragment,
        name="account_info_fragment",
    ),
    path(
        "accounts/<int:pk>/actions/<str:action>/",
        views.account_action,
        name="account_action",
    ),
    path(
        "accounts/<int:pk>/channels/",
        views.account_channel_selection,
        name="account_channel_selection",
    ),
    path("presets/", views.preset_list, name="preset_list"),
    path("presets/new/", views.preset_create, name="preset_create"),
    path("presets/<int:pk>/", views.preset_detail, name="preset_detail"),
    path("presets/<int:pk>/edit/", views.preset_edit, name="preset_edit"),
    path("presets/<int:pk>/delete/", views.preset_delete, name="preset_delete"),
    path("presets/<int:pk>/assign/", views.preset_assign, name="preset_assign"),
    path("settings/", views.settings_general, name="settings_general"),
    path("settings/import/", views.settings_import, name="settings_import"),
    path(
        "settings/import/confirm/",
        views.settings_import_confirm,
        name="settings_import_confirm",
    ),
    path(
        "settings/import/cancel/",
        views.settings_import_cancel,
        name="settings_import_cancel",
    ),
]
