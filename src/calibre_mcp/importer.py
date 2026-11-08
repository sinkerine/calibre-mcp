"""Import local files through Calibre's calibredb command."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger("calibre_mcp")
Duplicates = Literal["refuse", "new_record", "merge"]
DUPLICATES: tuple[str, ...] = ("refuse", "new_record", "merge")
LOCKED = "Another calibre program"
_DUPLICATE_HEADER = ("The following books were not added as they already exist in the database "
                     "(see --duplicates option or --automerge option):")


class AddError(RuntimeError):
    """A user-facing reason the book was not added."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BookMetadata:
    title: str | None = None
    authors: list[str] | None = None
    tags: list[str] | None = None
    series: str | None = None
    series_index: float | None = None
    languages: list[str] | None = None
    identifiers: dict[str, str] | None = None

    def args(self) -> list[str]:
        """Build calibredb options, rejecting ambiguous or empty values."""
        args = []
        if self.title is not None:
            title = self.title.strip()
            if not title:
                raise AddError("title must not be empty")
            args.extend(["--title", title])
        if self.authors:
            authors = [a.strip() for a in self.authors]
            if any(not a or "&" in a for a in authors):
                raise AddError("authors must not be empty or contain '&'")
            args.extend(["--authors", " & ".join(authors)])
        tags = [t.strip() for t in self.tags or [] if t.strip()]
        if any("," in t for t in tags):
            raise AddError("tags must not contain ','")
        if tags:
            args.extend(["--tags", ",".join(tags)])
        series = self.series.strip() if self.series else ""
        if series:
            args.extend(["--series", series])
        if self.series_index is not None:
            if not series:
                raise AddError("series_index needs series")
            args.extend(["--series-index", repr(float(self.series_index))])
        languages = [l.strip() for l in self.languages or [] if l.strip()]
        if any("," in l for l in languages):
            raise AddError("languages must not contain ','")
        if languages:
            args.extend(["--languages", ",".join(languages)])
        for key, value in sorted((self.identifiers or {}).items()):
            key, value = key.strip(), value.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                raise AddError("identifier keys must contain only letters, digits, '_' and '-'")
            if not value or "," in value:
                raise AddError("identifier values must not be empty or contain ','")
            args.extend(["--identifier", f"{key.lower()}:{value}"])
        return args


@dataclass
class AddResult:
    status: Literal["added", "merged", "duplicate"]
    book_ids: list[int]
    duplicate_titles: list[str]


def _diagnostic_tail(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    def text(output: str | bytes | None) -> str:
        if isinstance(output, bytes):
            return output.decode("utf-8", errors="replace").strip()
        return (output or "").strip()

    return " | ".join((text(stderr) or text(stdout)).splitlines()[-5:])


def parse_output(stdout: str, stderr: str = "") -> AddResult:
    """Read stdout results and Calibre's explicit duplicate report on stderr."""
    added, merged, titles = [], [], []
    duplicate = False
    for line in stdout.splitlines():
        match = re.fullmatch(r"(Added|Merged) book ids: (.+)", line)
        if match:
            try:
                ids = [int(i.strip()) for i in match[2].split(",")]
            except ValueError as exc:
                raise AddError("calibredb added nothing: " + _diagnostic_tail(stdout, stderr), 502) from exc
            (added if match[1] == "Added" else merged).extend(ids)
        elif "already exist in the database" in line:
            duplicate = True
        elif duplicate and line.startswith("  ") and not line.startswith("   "):
            titles.append(line.strip())
    if added or merged:
        return AddResult("added" if added else "merged", added + merged, titles)
    if duplicate:
        return AddResult("duplicate", [], titles)
    # Calibre 8.5 writes duplicate headers/titles to stderr, but paths to stdout.
    lines = stderr.splitlines()
    for index, line in enumerate(lines):
        if line == _DUPLICATE_HEADER:
            titles = []
            for title in lines[index + 1:]:
                if not title.startswith("  ") or title.startswith("   "):
                    break
                titles.append(title.strip())
            if titles:
                return AddResult("duplicate", [], titles)
    raise AddError("calibredb added nothing: " + _diagnostic_tail(stdout, stderr), 502)


class Importer:
    def __init__(self, exe: str, *, timeout: int = 300) -> None:
        self.exe = exe
        self._available = shutil.which(exe) is not None
        self.timeout = timeout
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def available(self) -> bool:
        return self._available

    def command(self, library_root: Path, src: Path, metadata: BookMetadata,
                duplicates: Duplicates = "refuse") -> list[str]:
        flags = {"refuse": [], "new_record": ["--duplicates"], "merge": ["--automerge", "ignore"]}
        if duplicates not in flags:
            raise AddError(f"unknown duplicates mode: {duplicates!r}")
        return [self.exe, "add", "--with-library", str(library_root), *flags[duplicates],
                *metadata.args(), "--", str(src)]

    async def add(self, library_root: Path, src: Path, metadata: BookMetadata,
                  duplicates: Duplicates = "refuse") -> AddResult:
        if not self.available:
            raise AddError("calibredb is not installed; adding books needs Calibre", 503)
        if not src.is_file():
            raise AddError(f"{str(src)!r} is not an existing regular file")
        if not src.suffix:
            raise AddError(f"{str(src)!r} has no file extension; Calibre picks the format from it")
        cmd = self.command(library_root, src, metadata, duplicates)
        lock = self._locks.setdefault(str(library_root), asyncio.Lock())
        async with lock:
            log.info("adding %s to %s", src.name, library_root)
            stdout, stderr = await asyncio.to_thread(self._run, cmd)
        return parse_output(stdout, stderr)

    def _run(self, cmd: list[str]) -> tuple[str, str]:
        with tempfile.TemporaryDirectory(prefix="calibre-mcp-add-") as tmp:
            env = dict(os.environ, HOME=tmp, QT_QPA_PLATFORM="offscreen")
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, env=env)
            except subprocess.TimeoutExpired as exc:
                tail = _diagnostic_tail(exc.stdout, exc.stderr)
                message = f"calibredb timed out after {self.timeout}s"
                raise AddError(message + (": " + tail if tail else ""), 502) from exc
        if LOCKED in proc.stdout or LOCKED in proc.stderr:
            raise AddError("the library is open in another Calibre program (desktop app or calibre-server); "
                           "close it and retry", 503)
        if proc.returncode != 0:
            raise AddError("calibredb failed: " + _diagnostic_tail(proc.stdout, proc.stderr), 502)
        return proc.stdout, proc.stderr
