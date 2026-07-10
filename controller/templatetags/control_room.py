"""Small presentation-only helpers for control-room templates."""

from __future__ import annotations

from collections.abc import Iterable

from django import template


register = template.Library()


@register.filter
def state_tone(value: object) -> str:
    """Map runtime vocabulary to a stable visual tone."""

    normalized = str(value or "unknown").lower()
    if normalized in {"running", "healthy", "recovered", "succeeded", "confirmed"}:
        return "good"
    if normalized in {"starting", "stopping", "restarting", "queued", "leased", "scheduled"}:
        return "pending"
    if normalized in {"degraded", "failed", "open", "unexpected_exit", "stale"}:
        return "danger"
    if normalized in {"stopped", "cancelled", "offline", "closed"}:
        return "idle"
    return "unknown"


@register.filter
def channel_list(value: object) -> str:
    """Render channel collections consistently without exposing raw JSON."""

    if not value:
        return "No channels"
    if isinstance(value, str):
        return value
    if isinstance(value, Iterable):
        return ", ".join(str(item) for item in value)
    return str(value)


@register.filter
def short_fingerprint(value: object) -> str:
    fingerprint = str(value or "")
    return fingerprint[:10] if fingerprint else "unavailable"
