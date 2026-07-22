"""Bounded live tails and durable per-account miner log archives."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any

from django.conf import settings
from django.core import signing


MAX_LOG_LINES = 400
MAX_LOG_BYTES = 256 * 1024
ACCOUNT_LOG_PART_BYTES = 5 * 1024 * 1024
ACCOUNT_LOG_ARCHIVE_BYTES = 50 * 1024 * 1024
LIVE_CURSOR_SALT = "twitch-farm.log-live.v1"
RUN_CURSOR_SALT = "twitch-farm.log-run.v1"
PART_PATTERN = re.compile(r"^part-(?P<sequence>\d{6})\.log(?P<gzip>\.gz)?$")


class LogStorageError(RuntimeError):
    """A redacted, operator-safe log storage failure."""


@dataclass(frozen=True, slots=True)
class LogPart:
    sequence: int
    path: Path
    compressed: bool


@dataclass(frozen=True, slots=True)
class RunLogSummary:
    available: bool
    compressed_bytes: int
    compressed_parts: int
    plaintext_parts: int
    truncated: bool
    compression_pending: bool


def _part_bytes() -> int:
    return int(getattr(settings, "TWITCH_FARM_ACCOUNT_LOG_PART_BYTES", ACCOUNT_LOG_PART_BYTES))


def _archive_bytes() -> int:
    return int(
        getattr(settings, "TWITCH_FARM_ACCOUNT_LOG_ARCHIVE_BYTES", ACCOUNT_LOG_ARCHIVE_BYTES)
    )


def _log_root() -> Path:
    return Path(settings.TWITCH_FARM_LOG_FILE).parent


def _account_root(account_id: int) -> Path:
    if not isinstance(account_id, int) or account_id <= 0:
        raise LogStorageError("The account log identifier is invalid.")
    return _log_root() / "accounts" / str(account_id)


def _run_root(account_id: int, run_id: int) -> Path:
    if not isinstance(run_id, int) or run_id <= 0:
        raise LogStorageError("The run log identifier is invalid.")
    return _account_root(account_id) / "runs" / str(run_id)


def _assert_not_symlink(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise LogStorageError("A log storage path is unsafe.")


def _ensure_private_directory(path: Path) -> None:
    root = _log_root()
    _assert_not_symlink(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = root
    relative = path.relative_to(root)
    for part in relative.parts:
        current = current / part
        _assert_not_symlink(current)
        current.mkdir(mode=0o700, exist_ok=True)
        current.chmod(0o700)


def _safe_regular_file(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except (FileNotFoundError, OSError):
        return False
    return stat.S_ISREG(mode)


def _open_private_text(path: Path):
    _assert_not_symlink(path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", buffering=1, newline="\n")


def _parse_part(path: Path) -> LogPart | None:
    match = PART_PATTERN.fullmatch(path.name)
    if not match or not _safe_regular_file(path):
        return None
    return LogPart(
        sequence=int(match.group("sequence")),
        path=path,
        compressed=bool(match.group("gzip")),
    )


def _run_parts(account_id: int, run_id: int) -> list[LogPart]:
    root = _run_root(account_id, run_id)
    _assert_not_symlink(root)
    if not root.is_dir():
        return []
    selected: dict[int, LogPart] = {}
    for path in root.iterdir():
        part = _parse_part(path)
        if part is None:
            continue
        previous = selected.get(part.sequence)
        if previous is None or part.compressed:
            selected[part.sequence] = part
    return sorted(selected.values(), key=lambda item: item.sequence)


def summarize_run_log(account_id: int, run_id: int) -> RunLogSummary:
    parts = _run_parts(account_id, run_id)
    compressed = [part for part in parts if part.compressed]
    plaintext = [part for part in parts if not part.compressed]
    return RunLogSummary(
        available=bool(parts),
        compressed_bytes=sum(part.path.stat().st_size for part in compressed),
        compressed_parts=len(compressed),
        plaintext_parts=len(plaintext),
        truncated=bool(parts and parts[0].sequence > 0),
        compression_pending=bool(plaintext),
    )


def _all_account_archives(account_id: int) -> list[LogPart]:
    runs_root = _account_root(account_id) / "runs"
    _assert_not_symlink(runs_root)
    if not runs_root.is_dir():
        return []
    parts: list[LogPart] = []
    for run_root in runs_root.iterdir():
        if not run_root.name.isdigit():
            continue
        _assert_not_symlink(run_root)
        if not run_root.is_dir():
            continue
        for path in run_root.iterdir():
            part = _parse_part(path)
            if part and part.compressed:
                parts.append(part)
    return sorted(parts, key=lambda item: (item.path.stat().st_mtime_ns, item.sequence))


def _archive_order(part: LogPart) -> tuple[int, int, int]:
    return (
        part.path.stat().st_mtime_ns,
        int(part.path.parent.name),
        part.sequence,
    )


def _prune_for_archive(
    account_id: int,
    reserve_bytes: int,
    *,
    candidate: LogPart | None = None,
) -> bool:
    limit = _archive_bytes()
    if reserve_bytes > limit:
        raise LogStorageError("A compressed log part exceeds the account archive limit.")
    archives = _all_account_archives(account_id)
    total = sum(part.path.stat().st_size for part in archives)
    candidate_retained = True
    ordered: list[tuple[tuple[int, int, int], LogPart | None]] = [
        (_archive_order(part), part) for part in archives
    ]
    if candidate is not None:
        ordered.append((_archive_order(candidate), None))
    for _order, part in sorted(ordered, key=lambda item: item[0]):
        if total + reserve_bytes <= limit:
            break
        if part is None:
            reserve_bytes = 0
            candidate_retained = False
            continue
        size = part.path.stat().st_size
        part.path.unlink()
        total -= size
        try:
            part.path.parent.rmdir()
        except OSError:
            pass
    if total + reserve_bytes > limit:
        raise LogStorageError("The account log archive could not be pruned safely.")
    return candidate_retained


def _compress_plain_part(account_id: int, path: Path) -> Path:
    part = _parse_part(path)
    if part is None or part.compressed:
        raise LogStorageError("The plaintext log part is invalid.")
    destination = Path(f"{path}.gz")
    temporary = Path(f"{destination}.{os.getpid()}.{threading.get_ident()}.tmp")
    _assert_not_symlink(destination)
    _assert_not_symlink(temporary)
    try:
        source_stat = path.stat()
        with path.open("rb") as source, temporary.open("xb") as raw_target:
            os.chmod(temporary, 0o600)
            with gzip.GzipFile(filename=path.name, mode="wb", fileobj=raw_target, mtime=0) as target:
                while chunk := source.read(128 * 1024):
                    target.write(chunk)
            raw_target.flush()
            os.fsync(raw_target.fileno())
        os.utime(
            temporary,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
        )
        retain_candidate = _prune_for_archive(
            account_id,
            temporary.stat().st_size,
            candidate=part,
        )
        if retain_candidate:
            os.replace(temporary, destination)
        else:
            temporary.unlink()
        path.unlink()
        if not retain_candidate:
            try:
                path.parent.rmdir()
            except OSError:
                pass
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _format_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    return current.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AccountRunLogWriter:
    """Thread-safe writer for one immutable account/run pair."""

    def __init__(self, *, account_id: int, run_id: int, account_key: str):
        self.account_id = account_id
        self.run_id = run_id
        self.account_key = account_key
        self.root = _run_root(account_id, run_id)
        _ensure_private_directory(self.root)
        self._lock = threading.RLock()
        self._stream = None
        self._path: Path | None = None
        self._sequence = max((part.sequence for part in _run_parts(account_id, run_id)), default=-1) + 1
        self._closed = False
        self.last_error = ""
        self._open_part()

    def _open_part(self) -> None:
        path = self.root / f"part-{self._sequence:06d}.log"
        self._stream = _open_private_text(path)
        self._path = path

    def _render(self, message: str, *, level: str, kind: str) -> str:
        clean_message = str(message).replace("\x00", "")[:8000]
        return (
            f"{_format_timestamp()} {level.upper()} {kind} "
            f"account={self.account_key} run={self.run_id}: {clean_message}"
        )

    def write(self, message: str, *, level: str = "INFO", kind: str = "miner") -> None:
        with self._lock:
            if self._closed or self._stream is None or self._path is None:
                return
            try:
                rendered = self._render(message, level=level, kind=kind) + "\n"
                part_limit = max(1, _part_bytes())
                if (
                    self._stream.tell() > 0
                    and self._stream.tell() + len(rendered.encode("utf-8")) > part_limit
                ):
                    try:
                        self._seal_part(open_next=True)
                    except Exception as exc:
                        self.last_error = type(exc).__name__
                    if self._stream is None:
                        return
                self._stream.write(rendered)
                if self._stream.tell() >= part_limit:
                    self._seal_part(open_next=True)
            except Exception as exc:
                self.last_error = type(exc).__name__

    def lifecycle(self, event: str, **details: Any) -> None:
        suffix = " ".join(
            f"{key}={json.dumps(value, ensure_ascii=True, separators=(',', ':'))}"
            for key, value in sorted(details.items())
            if value is not None and value != ""
        )
        message = event if not suffix else f"{event} {suffix}"
        self.write(message, kind="lifecycle")

    def _seal_part(self, *, open_next: bool) -> None:
        if self._stream is None or self._path is None:
            return
        stream = self._stream
        path = self._path
        self._stream = None
        self._path = None
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        try:
            _compress_plain_part(self.account_id, path)
        finally:
            if open_next:
                self._sequence += 1
                self._open_part()

    def finalize(self, event: str, **details: Any) -> None:
        with self._lock:
            if self._closed:
                return
            self.lifecycle(event, **details)
            try:
                try:
                    self._seal_part(open_next=False)
                except Exception as exc:
                    self.last_error = type(exc).__name__
            finally:
                self._closed = True


def recover_account_log_archives(*, active_run_ids: Iterable[int] = ()) -> list[str]:
    """Compress sealed plaintext parts and enforce retention for inactive runs."""

    active = {int(run_id) for run_id in active_run_ids}
    base = _log_root() / "accounts"
    _assert_not_symlink(base)
    if not base.is_dir():
        return []
    errors: list[str] = []
    for account_root in base.iterdir():
        if not account_root.name.isdigit():
            continue
        account_id = int(account_root.name)
        try:
            _assert_not_symlink(account_root)
            runs_root = account_root / "runs"
            _assert_not_symlink(runs_root)
            if not runs_root.is_dir():
                continue
            for run_root in runs_root.iterdir():
                if not run_root.name.isdigit() or int(run_root.name) in active:
                    continue
                _assert_not_symlink(run_root)
                if not run_root.is_dir():
                    continue
                for path in sorted(run_root.iterdir()):
                    part = _parse_part(path)
                    if part and not part.compressed:
                        try:
                            _compress_plain_part(account_id, path)
                        except Exception as exc:
                            errors.append(f"account={account_id} run={run_root.name}: {type(exc).__name__}")
            _prune_for_archive(account_id, 0)
        except Exception as exc:
            errors.append(f"account={account_id}: {type(exc).__name__}")
    return errors


def _read_file_tail(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            return stream.read(limit)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return b""


def _decode_runtime_tail(chunks: list[bytes]) -> list[str]:
    if not chunks:
        return []
    text = b"\n".join(chunks).decode("utf-8", errors="replace")
    text = text.encode("utf-8")[-MAX_LOG_BYTES:].decode("utf-8", errors="ignore")
    return text.splitlines()[-MAX_LOG_LINES:]


def _read_runtime_log_snapshot() -> tuple[list[str], list[int], int]:
    path = Path(settings.TWITCH_FARM_LOG_FILE)
    chunks: list[bytes] = []
    remaining = MAX_LOG_BYTES
    identity = [0, 0]
    offset = 0
    try:
        with path.open("rb") as stream:
            file_stat = os.fstat(stream.fileno())
            identity = [file_stat.st_dev, file_stat.st_ino]
            offset = file_stat.st_size
            amount = min(remaining, offset)
            stream.seek(offset - amount)
            chunk = stream.read(amount)
            if chunk:
                chunks.insert(0, chunk)
                remaining -= len(chunk)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        pass
    for candidate in (Path(f"{path}.1"), Path(f"{path}.2"), Path(f"{path}.3")):
        if remaining <= 0:
            break
        chunk = _read_file_tail(candidate, remaining)
        if chunk:
            chunks.insert(0, chunk)
            remaining -= len(chunk)
    return _decode_runtime_tail(chunks), identity, offset


def read_runtime_log_tail() -> list[str]:
    """Return the newest bounded lines across the current and rotated files."""

    lines, _identity, _offset = _read_runtime_log_snapshot()
    return lines


def _sign_cursor(payload: dict[str, Any], *, salt: str) -> str:
    return signing.dumps(payload, salt=salt, compress=True)


def _unsign_cursor(value: str, *, salt: str) -> dict[str, Any]:
    try:
        payload = signing.loads(value, salt=salt, max_age=7 * 24 * 60 * 60)
    except signing.BadSignature as exc:
        raise LogStorageError("The log cursor is invalid or expired.") from exc
    if not isinstance(payload, dict):
        raise LogStorageError("The log cursor is invalid or expired.")
    return payload


def _read_binary_lines(stream, *, max_lines: int, max_bytes: int) -> tuple[list[str], int]:
    lines: list[str] = []
    consumed = 0
    while len(lines) < max_lines and consumed < max_bytes:
        position = stream.tell()
        raw = stream.readline(max_bytes - consumed + 1)
        if not raw:
            break
        if consumed + len(raw) > max_bytes:
            stream.seek(position)
            break
        consumed += len(raw)
        lines.append(raw.rstrip(b"\r\n").decode("utf-8", errors="replace"))
    return lines, consumed


def read_combined_live(cursor: str | None = None) -> dict[str, Any]:
    path = Path(settings.TWITCH_FARM_LOG_FILE)
    if not cursor:
        lines, identity, offset = _read_runtime_log_snapshot()
        return {
            "lines": lines,
            "cursor": _sign_cursor(
                {"kind": "combined", "identity": identity, "offset": offset},
                salt=LIVE_CURSOR_SALT,
            ),
            "reset": False,
            "run_id": None,
        }

    payload = _unsign_cursor(cursor, salt=LIVE_CURSOR_SALT)
    if payload.get("kind") != "combined":
        raise LogStorageError("The log cursor does not match this source.")
    try:
        file_stat = path.stat()
    except OSError:
        return read_combined_live(None) | {"reset": True}
    identity = [file_stat.st_dev, file_stat.st_ino]
    offset = payload.get("offset")
    if payload.get("identity") != identity or not isinstance(offset, int) or offset > file_stat.st_size:
        return read_combined_live(None) | {"reset": True}
    with path.open("rb") as stream:
        stream.seek(offset)
        lines, consumed = _read_binary_lines(
            stream, max_lines=MAX_LOG_LINES, max_bytes=MAX_LOG_BYTES
        )
    return {
        "lines": lines,
        "cursor": _sign_cursor(
            {"kind": "combined", "identity": identity, "offset": offset + consumed},
            salt=LIVE_CURSOR_SALT,
        ),
        "reset": False,
        "run_id": None,
    }


def _read_part_lines(part: LogPart) -> list[str]:
    try:
        opener = gzip.open if part.compressed else open
        with opener(part.path, "rt", encoding="utf-8", errors="replace") as stream:
            return [line.rstrip("\r\n") for line in stream]
    except (OSError, EOFError) as exc:
        raise LogStorageError("A stored log part could not be read.") from exc


def _run_page_from_position(
    *,
    account_id: int,
    run_id: int,
    start_sequence: int | None,
    start_line: int | None,
    max_lines: int = MAX_LOG_LINES,
    max_bytes: int = MAX_LOG_BYTES,
) -> dict[str, Any]:
    parts = _run_parts(account_id, run_id)
    if not parts:
        raise LogStorageError("No retained log is available for this run.")
    by_sequence = {part.sequence: part for part in parts}
    sequence = parts[-1].sequence if start_sequence is None else start_sequence
    if sequence not in by_sequence:
        raise LogStorageError("The requested log position has expired.")
    part_sequences = [part.sequence for part in parts]
    chunks: list[list[str]] = []
    remaining_lines = max(1, min(max_lines, MAX_LOG_LINES))
    remaining_bytes = max_bytes
    next_before: str | None = None
    end_sequence = sequence
    end_line = 0
    captured_end = False
    while remaining_lines > 0 and remaining_bytes > 0:
        part = by_sequence[sequence]
        lines = _read_part_lines(part)
        end = len(lines) if start_line is None else min(max(start_line, 0), len(lines))
        if not captured_end:
            end_sequence = sequence
            end_line = end
            captured_end = True
        start = end
        used_bytes = 0
        while start > 0 and end - start < remaining_lines:
            encoded_size = len(lines[start - 1].encode("utf-8")) + 1
            if used_bytes + encoded_size > remaining_bytes:
                break
            start -= 1
            used_bytes += encoded_size
        if start < end:
            chunks.insert(0, lines[start:end])
            remaining_lines -= end - start
            remaining_bytes -= used_bytes
        previous_sequences = [item for item in part_sequences if item < sequence]
        if start > 0 or previous_sequences:
            next_before = _sign_cursor(
                {"run_id": run_id, "sequence": sequence, "line": start},
                salt=RUN_CURSOR_SALT,
            )
        else:
            next_before = None
        if start > 0 or not previous_sequences or remaining_lines <= 0 or remaining_bytes <= 0:
            break
        sequence = previous_sequences[-1]
        start_line = None
    return {
        "lines": [line for chunk in chunks for line in chunk],
        "before": next_before,
        "has_older": next_before is not None,
        "_end_sequence": end_sequence,
        "_end_line": end_line,
    }


def read_run_log_page(
    *, account_id: int, run_id: int, before: str | None = None, limit: int = MAX_LOG_LINES
) -> dict[str, Any]:
    sequence = None
    line = None
    if before:
        payload = _unsign_cursor(before, salt=RUN_CURSOR_SALT)
        if payload.get("run_id") != run_id:
            raise LogStorageError("The log cursor does not match this run.")
        sequence = payload.get("sequence")
        line = payload.get("line")
        if not isinstance(sequence, int) or not isinstance(line, int):
            raise LogStorageError("The log cursor is invalid or expired.")
    return _run_page_from_position(
        account_id=account_id,
        run_id=run_id,
        start_sequence=sequence,
        start_line=line,
        max_lines=limit,
    )


def _account_live_cursor(run_id: int, sequence: int, line: int) -> str:
    return _sign_cursor(
        {"kind": "account", "run_id": run_id, "sequence": sequence, "line": line},
        salt=LIVE_CURSOR_SALT,
    )


def read_account_live(
    *, account_id: int, run_id: int, cursor: str | None = None
) -> dict[str, Any]:
    parts = _run_parts(account_id, run_id)
    if not parts:
        return {
            "lines": [],
            "cursor": _account_live_cursor(run_id, 0, 0),
            "reset": False,
            "run_id": run_id,
        }
    if not cursor:
        page = read_run_log_page(account_id=account_id, run_id=run_id)
        return {
            "lines": page["lines"],
            "cursor": _account_live_cursor(
                run_id,
                page["_end_sequence"],
                page["_end_line"],
            ),
            "reset": False,
            "run_id": run_id,
        }
    payload = _unsign_cursor(cursor, salt=LIVE_CURSOR_SALT)
    if payload.get("kind") != "account" or payload.get("run_id") != run_id:
        return read_account_live(account_id=account_id, run_id=run_id) | {"reset": True}
    sequence = payload.get("sequence")
    line_index = payload.get("line")
    if not isinstance(sequence, int) or not isinstance(line_index, int):
        raise LogStorageError("The log cursor is invalid or expired.")
    by_sequence = {part.sequence: part for part in parts}
    if sequence not in by_sequence:
        return read_account_live(account_id=account_id, run_id=run_id) | {"reset": True}
    lines: list[str] = []
    used_bytes = 0
    last_sequence = sequence
    last_line = line_index
    for part in (part for part in parts if part.sequence >= sequence):
        part_lines = _read_part_lines(part)
        start = line_index if part.sequence == sequence else 0
        start = min(max(start, 0), len(part_lines))
        for index in range(start, len(part_lines)):
            size = len(part_lines[index].encode("utf-8")) + 1
            if len(lines) >= MAX_LOG_LINES or used_bytes + size > MAX_LOG_BYTES:
                return {
                    "lines": lines,
                    "cursor": _sign_cursor(
                        {
                            "kind": "account",
                            "run_id": run_id,
                            "sequence": part.sequence,
                            "line": index,
                        },
                        salt=LIVE_CURSOR_SALT,
                    ),
                    "reset": False,
                    "run_id": run_id,
                }
            lines.append(part_lines[index])
            used_bytes += size
            last_sequence = part.sequence
            last_line = index + 1
    return {
        "lines": lines,
        "cursor": _sign_cursor(
            {
                "kind": "account",
                "run_id": run_id,
                "sequence": last_sequence,
                "line": last_line,
            },
            salt=LIVE_CURSOR_SALT,
        ),
        "reset": False,
        "run_id": run_id,
    }


def iter_run_gzip(account_id: int, run_id: int) -> tuple[Iterator[bytes], int]:
    parts = _run_parts(account_id, run_id)
    if not parts or any(not part.compressed for part in parts):
        raise LogStorageError("The completed run archive is not ready for download.")
    total = sum(part.path.stat().st_size for part in parts)
    streams = []
    try:
        for part in parts:
            if not _safe_regular_file(part.path):
                raise LogStorageError("A retained log part is no longer available.")
            streams.append(part.path.open("rb"))
    except Exception:
        for stream in streams:
            stream.close()
        raise

    def chunks() -> Iterator[bytes]:
        try:
            for stream in streams:
                while chunk := stream.read(128 * 1024):
                    yield chunk
        finally:
            for stream in streams:
                stream.close()

    return chunks(), total
