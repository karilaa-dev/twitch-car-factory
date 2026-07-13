"""Bounded, read-only access to the worker-owned combined runtime log."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings


MAX_LOG_LINES = 400
MAX_LOG_BYTES = 256 * 1024


def _read_file_tail(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            return stream.read(limit)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return b""


def read_runtime_log_tail() -> list[str]:
    """Return the newest bounded lines across the current and rotated files."""

    path = Path(settings.TWITCH_FARM_LOG_FILE)
    chunks: list[bytes] = []
    remaining = MAX_LOG_BYTES
    for candidate in (path, Path(f"{path}.1"), Path(f"{path}.2"), Path(f"{path}.3")):
        if remaining <= 0:
            break
        chunk = _read_file_tail(candidate, remaining)
        if chunk:
            chunks.insert(0, chunk)
            remaining -= len(chunk)
    if not chunks:
        return []
    text = b"\n".join(chunks).decode("utf-8", errors="replace")
    text = text.encode("utf-8")[-MAX_LOG_BYTES:].decode("utf-8", errors="ignore")
    return text.splitlines()[-MAX_LOG_LINES:]
