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
    path("accounts/", views.account_list, name="account_list"),
    path("accounts/<int:pk>/", views.account_detail, name="account_detail"),
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
]
