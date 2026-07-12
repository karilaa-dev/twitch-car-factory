"""Root routes for the Twitch Farm controller."""

from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("controller.urls")),
]
