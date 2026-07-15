"""Routes for the Django-hosted React control room."""

from django.urls import include, path

from . import views


app_name = "controller"

urlpatterns = [
    path("healthz/", views.healthz, name="healthz"),
    path("api/v1/", include("controller.api_urls")),
    path("login", views.spa, name="login"),
    path("", views.spa, name="dashboard"),
    path("accounts", views.spa, name="account_list"),
    path("accounts/new", views.spa, name="account_create"),
    path("accounts/<int:pk>", views.spa, name="account_detail"),
    path("presets", views.spa, name="preset_list"),
    path("presets/new", views.spa, name="preset_create"),
    path("presets/<int:pk>", views.spa, name="preset_detail"),
    path("logs", views.spa, name="logs"),
    path("settings", views.spa, name="settings"),
]
