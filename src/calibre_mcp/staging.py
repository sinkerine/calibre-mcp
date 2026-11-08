"""Single-process staging registry, confined to one asyncio event loop."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from starlette.requests import Request

State = Literal["pending", "uploading", "ready", "importing"]
ID_RE = re.compile(r"[A-Za-z0-9_-]{32}")
_UNKNOWN = "unknown or expired file_id; call prepare_upload again"
_BUSY = "file_id is busy (an upload or import is in progress); retry shortly"


class StagingError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def safe_filename(name: str) -> str:
    """Keep a sanitised basename with a non-empty stem and extension."""
    name = re.split(r"[/\\]", name)[-1]
    name = re.sub(r'[\x00-\x1f\x7f-\x9f/:*?"<>|]', "", name).strip(" .")
    stem, dot, extension = name.rpartition(".")
    stem = stem.strip(" .")
    if not dot or not stem or not extension:
        raise StagingError("filename must have a non-empty stem and extension")
    return f"{stem[:150]}.{extension}"


@dataclass
class Staged:
    file_id: str
    filename: str
    dir: Path
    state: State
    expires: float
    size: int = 0
    sha256: str | None = None
    has_bytes: bool = False

    @property
    def path(self) -> Path:
        return self.dir / self.filename


class Stager:
    def __init__(self, root: Path, *, ttl: int, max_bytes: int, max_entries: int,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.root = root
        self.ttl = ttl
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.clock = clock
        self.wall = wall
        self._entries: dict[str, Staged] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def _lookup(self, file_id: str) -> Staged:
        if not ID_RE.fullmatch(file_id) or file_id not in self._entries:
            raise StagingError(_UNKNOWN, 404)
        return self._entries[file_id]

    def prepare(self, filename: str) -> Staged:
        self.reap()
        filename = safe_filename(filename)
        if len(self) >= self.max_entries:
            raise StagingError(f"too many pending uploads ({self.max_entries}); import or wait for them to expire", 429)
        file_id = secrets.token_urlsafe(24)
        directory = self.root / f".staged-{file_id}"
        directory.mkdir(parents=True)
        staged = Staged(file_id, filename, directory, "pending", self.clock() + self.ttl)
        self._entries[file_id] = staged
        return staged

    async def receive(self, file_id: str, request: Request, max_bytes: int | None = None) -> Staged:
        self.reap()
        staged = self._lookup(file_id)
        if staged.state in ("uploading", "importing"):
            raise StagingError(_BUSY, 409)
        cap = self.max_bytes if max_bytes is None else max_bytes
        message = f"upload larger than {cap / (1024 * 1024):g} MB"
        length = request.headers.get("content-length")
        if length is not None:
            if not re.fullmatch(r"[0-9]+", length):
                raise StagingError("invalid Content-Length")
            if int(length) > cap:
                raise StagingError(message, 413)
        staged.state = "uploading"
        part = staged.dir / ".part"
        completed = False
        try:
            total = 0
            digest = hashlib.sha256()
            with part.open("wb") as dest:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > cap:
                        raise StagingError(message, 413)
                    digest.update(chunk)
                    for offset in range(0, len(chunk), 1024 * 1024):
                        dest.write(chunk[offset:offset + 1024 * 1024])
            if total == 0:
                raise StagingError("empty upload")
            if self._entries.get(file_id) is not staged:
                raise StagingError(_UNKNOWN, 404)
            os.replace(part, staged.path)
            staged.size = total
            staged.sha256 = digest.hexdigest()
            staged.has_bytes = True
            staged.state = "ready"
            staged.expires = self.clock() + self.ttl
            completed = True
            return staged
        finally:
            if not completed:
                part.unlink(missing_ok=True)
                staged.state = "ready" if staged.has_bytes else "pending"
                if self._entries.get(file_id) is not staged or staged.expires <= self.clock():
                    if self._entries.get(file_id) is staged:
                        del self._entries[file_id]
                    shutil.rmtree(staged.dir, ignore_errors=True)

    def claim(self, file_id: str) -> Staged:
        self.reap()
        staged = self._lookup(file_id)
        if staged.state == "pending":
            raise StagingError("no bytes uploaded yet for this file_id; PUT the file to upload_url first", 409)
        if staged.state in ("uploading", "importing"):
            raise StagingError(_BUSY, 409)
        staged.state = "importing"
        return staged

    def release(self, file_id: str) -> Staged | None:
        staged = self._entries.get(file_id)
        if staged is not None and staged.state == "importing":
            staged.state = "ready"
            staged.expires = self.clock() + self.ttl
            return staged
        return None

    def consume(self, file_id: str) -> None:
        staged = self._entries.pop(file_id, None)
        if staged is not None:
            shutil.rmtree(staged.dir, ignore_errors=True)

    def expires_at(self, staged: Staged) -> str:
        timestamp = self.wall() + (staged.expires - self.clock())
        return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def expires_in(self, staged: Staged) -> int:
        return max(0, int(staged.expires - self.clock()))

    def reap(self) -> int:
        expired = [file_id for file_id, staged in self._entries.items()
                   if staged.state in ("pending", "ready") and staged.expires <= self.clock()]
        for file_id in expired:
            self.consume(file_id)
        return len(expired)

    def sweep(self) -> None:
        """Remove crash leftovers before accepting requests."""
        for pattern in (".staged-*", ".upload-*"):
            for path in self.root.glob(pattern):
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
