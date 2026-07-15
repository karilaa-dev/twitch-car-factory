"""Django-owned liveness and React application shell views."""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe


@require_safe
def healthz(request: HttpRequest) -> HttpResponse:
    """Liveness only: deliberately exposes no controller or account state."""

    return HttpResponse("ok\n", content_type="text/plain; charset=utf-8")


@never_cache
@require_safe
def spa(request: HttpRequest, **_route_params) -> HttpResponse:
    """Serve the same React entry point for every operator-facing route."""

    return render(request, "controller/spa.html")
