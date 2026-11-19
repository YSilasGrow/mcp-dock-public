"""Private, append-only events shared by MCP calls and the local workbench."""

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_EVENTS = Path(__file__).resolve().parent.parent / ".local" / "workbench" / "events.jsonl"
_EVENT_CACHE = {}


def _path() -> Path:
    return Path(os.environ.get("DEEPSEEK_BRIDGE_EVENTS", str(DEFAULT_EVENTS))).expanduser()


def _new_event(data: dict) -> dict:
    return {"id": uuid.uuid4().hex, "at": datetime.now(timezone.utc).isoformat(),
            "call_id": "", **data}


@contextmanager
def _locked_file(exclusive: bool, create: bool = False):
    path = _path()
    if create:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(path.parent, 0o700)
    elif not path.exists():
        yield None
        return
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        if create:
            os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _complete_end(fd: int) -> int:
    size = os.fstat(fd).st_size
    if size == 0 or os.pread(fd, 1, size - 1) == b"\n":
        return size
    cursor = size
    while cursor:
        start = max(0, cursor - 65536)
        block = os.pread(fd, cursor - start, start)
        pos = block.rfind(b"\n")
        if pos >= 0:
            return start + pos + 1
        cursor = start
    return 0


def _read_rows(fd: int, start: int = 0, limit: int | None = None):
    end = _complete_end(fd)
    cursor = min(start, end)
    rows = []
    with os.fdopen(os.dup(fd), "rb", buffering=65536) as stream:
        stream.seek(cursor)
        while cursor < end and (limit is None or len(rows) < limit):
            line = stream.readline(end - cursor)
            cursor = stream.tell()
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows, cursor


def _cached_rows(fd: int):
    """Refresh only appended complete lines while the caller holds flock."""
    key = str(_path())
    stat = os.fstat(fd)
    end = _complete_end(fd)
    cached = _EVENT_CACHE.get(key)
    if cached and cached[0] == stat.st_ino and cached[1] <= end:
        rows = list(cached[2])
        added, cursor = _read_rows(fd, cached[1])
        if added:
            rows.extend(added)
    else:
        rows, cursor = _read_rows(fd)
    if len(_EVENT_CACHE) >= 16 and key not in _EVENT_CACHE:
        _EVENT_CACHE.pop(next(iter(_EVENT_CACHE)))
    _EVENT_CACHE[key] = (stat.st_ino, cursor, rows)
    return rows


def _cache_appended(fd: int, event: dict):
    key = str(_path())
    cached = _EVENT_CACHE.get(key)
    if cached and cached[0] == os.fstat(fd).st_ino:
        cached[2].append(event)
        _EVENT_CACHE[key] = (cached[0], _complete_end(fd), cached[2])


def _write_line(fd: int, event: dict):
    encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    os.lseek(fd, 0, os.SEEK_END)
    count = os.write(fd, encoded)
    if count != len(encoded):
        raise OSError(f"short event write: {count}/{len(encoded)}")


def _recover_tail(fd: int):
    end = _complete_end(fd)
    size = os.fstat(fd).st_size
    if end == size:
        return
    tail = os.pread(fd, size - end, end)
    path = _path()
    quarantine = path.with_name(path.name + f".partial-{uuid.uuid4().hex}.bin")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    qfd = os.open(quarantine, flags, 0o600)
    try:
        if os.write(qfd, tail) != len(tail):
            raise OSError("short quarantine write")
    finally:
        os.close(qfd)
    os.ftruncate(fd, end)
    _write_line(fd, _new_event({"role": "system", "kind": "recovery",
                                "text": "已隔离一条未写完的日志记录", "quarantine": quarantine.name}))


def append_event(role: str, kind: str, text: str, call_id: str | None = None) -> dict:
    if not isinstance(text, str):
        raise TypeError("event text must be a string")
    event = _new_event({"role": role, "kind": kind, "text": text, "call_id": call_id or ""})
    with _locked_file(True, create=True) as fd:
        _recover_tail(fd)
        _cached_rows(fd)
        _write_line(fd, event)
        _cache_appended(fd, event)
    return event


def append_research_event(candidate: dict, validate_existing, *, with_reused=False) -> dict:
    """Validate and append atomically with all legacy writers; reuse a matching event."""
    with _locked_file(True, create=True) as fd:
        _recover_tail(fd)
        rows = _cached_rows(fd)
        existing = validate_existing(rows)
        if existing is not None:
            return (existing, True) if with_reused else existing
        event = _new_event(candidate)
        _write_line(fd, event)
        _cache_appended(fd, event)
        return (event, False) if with_reused else event


def append_validated_event(build) -> dict:
    """Build one event from a consistent journal snapshot and append it under the same lock."""
    with _locked_file(True, create=True) as fd:
        _recover_tail(fd)
        event = _new_event(build(_cached_rows(fd)))
        _write_line(fd, event)
        _cache_appended(fd, event)
        return event


def read_all_events() -> list[dict]:
    with _locked_file(False) as fd:
        return [] if fd is None else list(_cached_rows(fd))


def read_events(after: int = 0) -> dict:
    if not isinstance(after, int) or after < 0:
        raise ValueError("after must be a non-negative integer")
    with _locked_file(False) as fd:
        if fd is None:
            return {"events": [], "next": 0}
        rows, cursor = _read_rows(fd, after, 200)
        return {"events": rows, "next": cursor}
