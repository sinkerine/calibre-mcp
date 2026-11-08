"""Configuration: a TOML file, then CALIBRE_MCP_* environment variables, then command-line flags.

    host = "0.0.0.0"
    port = 8000
    public_url = ""            # base for the links tools return; empty = from the request
    cache_dir = "/cache"       # converted EPUBs
    convert = true             # offer EPUB for books without one, via Calibre's ebook-convert
    ebook_convert = "ebook-convert"
    convert_timeout = 900      # seconds
    allowed_hosts = []         # Host headers /mcp accepts; empty = any (the proxy decides who gets in)
    calibredb = "calibredb"     # executable used to add books
    add_timeout = 300          # seconds
    max_upload_mb = 1024       # maximum upload size in MiB

    [[libraries]]              # one or more Calibre libraries (directories holding metadata.db)
    name = "main"
    path = "/books"

Environment: CALIBRE_MCP_CONFIG (file), CALIBRE_MCP_LIBRARIES ("main=/books,other=/other"),
CALIBRE_MCP_HOST, CALIBRE_MCP_PORT, CALIBRE_MCP_PUBLIC_URL, CALIBRE_MCP_CACHE_DIR,
CALIBRE_MCP_CONVERT (0/1), CALIBRE_MCP_EBOOK_CONVERT, CALIBRE_MCP_ALLOWED_HOSTS (comma-separated),
CALIBRE_MCP_CALIBREDB, CALIBRE_MCP_MAX_UPLOAD_MB (integer).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass
class LibraryConfig:
    name: str
    path: Path


@dataclass
class Config:
    libraries: list[LibraryConfig] = field(default_factory=list)
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str = ""
    cache_dir: Path = Path("cache")
    convert: bool = True
    ebook_convert: str = "ebook-convert"
    convert_timeout: int = 900
    allowed_hosts: list[str] = field(default_factory=list)
    calibredb: str = "calibredb"
    add_timeout: int = 300
    max_upload_mb: int = 1024

    def validate(self) -> None:
        if not self.libraries:
            raise SystemExit("no Calibre library configured ([[libraries]] or CALIBRE_MCP_LIBRARIES)")
        seen = set()
        for lib in self.libraries:
            if not NAME_RE.match(lib.name):
                raise SystemExit(f"bad library name {lib.name!r} (letters, digits, - and _)")
            if lib.name in seen:
                raise SystemExit(f"duplicate library name {lib.name!r}")
            seen.add(lib.name)
            if not (lib.path / "metadata.db").is_file():
                raise SystemExit(f"library {lib.name!r}: no metadata.db in {lib.path}")
        if self.max_upload_mb < 1:
            raise SystemExit("max_upload_mb must be at least 1")
        if self.add_timeout < 1:
            raise SystemExit("add_timeout must be at least 1")


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _libraries(spec: str) -> list[LibraryConfig]:
    out = []
    for item in filter(None, (s.strip() for s in spec.split(","))):
        name, sep, path = item.partition("=")
        if not sep:
            name, path = Path(item).name or "main", item
        out.append(LibraryConfig(name.strip(), Path(path.strip()).expanduser()))
    return out


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    raw: dict = {}
    path = path or (Path(env["CALIBRE_MCP_CONFIG"]) if env.get("CALIBRE_MCP_CONFIG") else None)
    if path:
        with Path(path).expanduser().open("rb") as fh:
            raw = tomllib.load(fh)
    cfg = Config(
        libraries=[LibraryConfig(str(lib["name"]), Path(lib["path"]).expanduser()) for lib in raw.get("libraries", [])],
        host=str(raw.get("host", "0.0.0.0")),
        port=int(raw.get("port", 8000)),
        public_url=str(raw.get("public_url", "")),
        cache_dir=Path(raw.get("cache_dir", "cache")).expanduser(),
        convert=bool(raw.get("convert", True)),
        ebook_convert=str(raw.get("ebook_convert", "ebook-convert")),
        convert_timeout=int(raw.get("convert_timeout", 900)),
        allowed_hosts=list(raw.get("allowed_hosts", [])),
        calibredb=str(raw.get("calibredb", "calibredb")),
        add_timeout=int(raw.get("add_timeout", 300)),
        max_upload_mb=int(raw.get("max_upload_mb", 1024)),
    )
    if v := env.get("CALIBRE_MCP_LIBRARIES"):
        cfg.libraries = _libraries(v)
    if v := env.get("CALIBRE_MCP_HOST"):
        cfg.host = v
    if v := env.get("CALIBRE_MCP_PORT"):
        cfg.port = int(v)
    if v := env.get("CALIBRE_MCP_PUBLIC_URL"):
        cfg.public_url = v
    if v := env.get("CALIBRE_MCP_CACHE_DIR"):
        cfg.cache_dir = Path(v).expanduser()
    if v := env.get("CALIBRE_MCP_CONVERT"):
        cfg.convert = _bool(v)
    if v := env.get("CALIBRE_MCP_EBOOK_CONVERT"):
        cfg.ebook_convert = v
    if v := env.get("CALIBRE_MCP_ALLOWED_HOSTS"):
        cfg.allowed_hosts = [h.strip() for h in v.split(",") if h.strip()]
    if v := env.get("CALIBRE_MCP_CALIBREDB"):
        cfg.calibredb = v
    if v := env.get("CALIBRE_MCP_MAX_UPLOAD_MB"):
        cfg.max_upload_mb = int(v)
    cfg.public_url = cfg.public_url.rstrip("/")
    return cfg
