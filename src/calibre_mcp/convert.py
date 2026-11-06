"""On-demand EPUB conversion with Calibre's ebook-convert, cached on disk."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("calibre_mcp")


class ConvertError(RuntimeError):
    pass


class Converter:
    def __init__(self, exe: str, cache_dir: Path, *, timeout: int = 900, parallel: int = 2) -> None:
        self.exe = shutil.which(exe)
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self._sem = asyncio.Semaphore(parallel)
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def available(self) -> bool:
        return self.exe is not None

    def cached(self, library: str, book_id: int, src: Path) -> Path:
        st = src.stat()
        key = hashlib.sha256(f"{src}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:16]
        return self.cache_dir / library / f"{book_id}-{key}.epub"

    async def to_epub(self, library: str, book_id: int, src: Path) -> Path:
        if not self.exe:
            raise ConvertError("ebook-convert is not installed")
        out = self.cached(library, book_id, src)
        if out.is_file():
            return out
        lock = self._locks.setdefault(str(out), asyncio.Lock())
        async with lock:
            if out.is_file():
                return out
            async with self._sem:
                await asyncio.to_thread(self._run, src, out)
        return out

    def _run(self, src: Path, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        for old in out.parent.glob(out.name.split("-")[0] + "-*.epub"):  # earlier versions of this book
            old.unlink(missing_ok=True)
        log.info("converting %s → EPUB", src.name)
        with tempfile.TemporaryDirectory(dir=out.parent, prefix=".convert-") as tmp:
            # ebook-convert picks the input plugin by extension; give it a plain one.
            ext = src.suffix.lower()
            if ext == ".original_epub":
                ext = ".epub"
            link = Path(tmp) / f"book{ext}"
            os.symlink(src, link)
            target = Path(tmp) / "book.epub"
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", HOME=tmp)
            try:
                proc = subprocess.run([self.exe, str(link), str(target)], capture_output=True, text=True,
                                      timeout=self.timeout, env=env)
            except subprocess.TimeoutExpired as exc:
                raise ConvertError(f"conversion timed out after {self.timeout}s") from exc
            if proc.returncode != 0 or not target.is_file():
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
                raise ConvertError("ebook-convert failed: " + " | ".join(tail))
            os.replace(target, out)
