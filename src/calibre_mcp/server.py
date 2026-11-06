"""calibre-mcp: MCP tools over Calibre libraries, plus direct HTTP links to every book file.

HTTP (no auth; put it behind a proxy that limits who can reach it):

    GET /                                      libraries and endpoints (JSON)
    GET /health
    GET /<library>/books/<id>                  metadata (JSON)
    GET /<library>/books/<id>/<format>         a book file, e.g. /main/books/1037/epub, /main/books/12/mobi
    GET /<library>/books/<id>/epub             the EPUB, converted on demand when the book has none
    GET /<library>/books/<id>/cover.jpg
    POST /mcp                                  MCP (streamable HTTP, stateless)
"""

from __future__ import annotations

import logging
import mimetypes
import re
import unicodedata
from collections.abc import Mapping
from typing import Literal
from urllib.parse import quote

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from . import __version__
from .config import Config
from .convert import ConvertError, Converter
from .library import Library

log = logging.getLogger("calibre_mcp")

INSTRUCTIONS = """\
Read-only access to Calibre ebook libraries. Find books with search_books (words match title, authors, tags
and series; filters narrow it), inspect one with get_book, and browse authors/tags/series with
browse_library. Every book comes with direct HTTP download URLs for each stored format, an `epub_url`
(converted on demand when the book has no EPUB; the first request can take a minute) and a cover URL.
Book ids are per library; omit `library` to use the default one."""
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True)
MEDIA = {".epub": "application/epub+zip", ".mobi": "application/x-mobipocket-ebook",
         ".azw3": "application/vnd.amazon.ebook", ".azw": "application/vnd.amazon.ebook",
         ".cbz": "application/vnd.comicbook+zip", ".cbr": "application/vnd.comicbook-rar",
         ".fb2": "application/x-fictionbook+xml", ".original_epub": "application/epub+zip"}


def disposition(name: str, fallback: str) -> str:
    """attachment; with an ASCII filename= (curl -J, wget) and the full UTF-8 filename* (browsers)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r'[^A-Za-z0-9._ ()-]+', "_", ascii_name).strip(" ._")
    if not re.search(r"[A-Za-z0-9]", ascii_name.rsplit(".", 1)[0]):
        ascii_name = fallback
    return f"attachment; filename=\"{ascii_name}\"; filename*=utf-8''{quote(name)}"


class Catalog:
    """The configured libraries, the converter and URL building."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.libraries = {lib.name: Library(lib.name, lib.path) for lib in cfg.libraries}
        self.default = cfg.libraries[0].name
        self.converter = Converter(cfg.ebook_convert, cfg.cache_dir, timeout=cfg.convert_timeout)
        if cfg.convert and not self.converter.available:
            log.warning("ebook-convert not found; books without an EPUB get no epub_url")

    @property
    def can_convert(self) -> bool:
        return self.cfg.convert and self.converter.available

    def library(self, name: str | None) -> Library:
        lib = self.libraries.get(name or self.default)
        if lib is None:
            raise KeyError(f"no library {name!r}; libraries: {', '.join(self.libraries)}")
        return lib

    def base_url(self, headers: Mapping[str, str] | None) -> str:
        if self.cfg.public_url:
            return self.cfg.public_url
        h = {k.lower(): v for k, v in (headers or {}).items()}
        host = (h.get("x-forwarded-host") or h.get("host") or f"127.0.0.1:{self.cfg.port}").split(",")[0].strip()
        proto = (h.get("x-forwarded-proto") or "http").split(",")[0].strip()
        return f"{proto}://{host}"

    def links(self, lib: Library, book: dict, base: str) -> dict:
        root = f"{base}/{lib.name}/books/{book['id']}"
        fmts = book["formats"]
        out = {"library": lib.name, "page": root,
               "downloads": {f: f"{root}/{f.lower()}" for f in fmts}}
        source = lib.epub_source(book["id"], fmts)
        if source and (source[0] == "original" or self.can_convert):
            out["epub_url"] = f"{root}/epub"
            out["epub_from"] = "original" if source[0] == "original" else f"converted from {source[1]}"
        if book.get("has_cover"):
            out["cover_url"] = f"{root}/cover.jpg"
        return out


def build_app(cfg: Config) -> Starlette:
    cfg.validate()
    cat = Catalog(cfg)
    mcp = MCPServer("calibre-mcp", instructions=INSTRUCTIONS, version=__version__)

    def lib_or_error(name: str | None) -> Library:
        try:
            return cat.library(name)
        except KeyError as exc:
            raise ToolError(str(exc)) from None

    # -- MCP tools ------------------------------------------------------------------

    @mcp.tool(annotations=READ)
    def list_libraries() -> dict:
        """The Calibre libraries served here, with book counts and stored formats."""
        return {"default": cat.default, "convert_to_epub": cat.can_convert,
                "libraries": [{"name": lib.name, "books": lib.count(), "formats": lib.formats()}
                              for lib in cat.libraries.values()]}

    @mcp.tool(annotations=READ)
    def search_books(ctx: Context, query: str | None = None, author: str | None = None, tag: str | None = None,
                     series: str | None = None, language: str | None = None, format: str | None = None,
                     sort: Literal["relevance", "added", "title", "author", "published", "modified"] = "relevance",
                     limit: int = 20, offset: int = 0, library: str | None = None) -> dict:
        """Find books. `query` words must each match the title, an author, a tag or the series (a number also
        matches the book id). Filters: author/tag/series substrings, language code (eng, zho, …), stored
        format (EPUB, MOBI, PDF, …). With no query, newest additions come first. Results include links."""
        lib = lib_or_error(library)
        total, books = lib.search(query, author=author, tag=tag, series=series, language=language,
                                  format=format, sort=sort, limit=limit, offset=offset)
        base = cat.base_url(ctx.headers)
        for b in books:
            b.update(cat.links(lib, b, base))
        return {"library": lib.name, "total": total, "offset": offset, "books": books}

    @mcp.tool(annotations=READ)
    def get_book(book_id: int, ctx: Context, library: str | None = None) -> dict:
        """Full metadata of one book (description, publisher, identifiers, files) with direct download URLs
        for every format, an EPUB URL (converted on demand if needed) and the cover URL."""
        lib = lib_or_error(library)
        book = lib.book(book_id)
        if book is None:
            raise ToolError(f"no book {book_id} in library {lib.name!r}")
        book.update(cat.links(lib, book, cat.base_url(ctx.headers)))
        return book

    @mcp.tool(annotations=READ)
    def browse_library(kind: Literal["authors", "tags", "series", "publishers", "languages"],
                       filter: str | None = None, limit: int = 50, library: str | None = None) -> dict:
        """Authors, tags, series, publishers or languages with book counts (most books first); `filter` is a
        substring. Use the names as search_books filters."""
        lib = lib_or_error(library)
        return {"library": lib.name, "kind": kind, "items": lib.browse(kind, filter, limit)}

    # -- HTTP -------------------------------------------------------------------------

    def not_found(msg: str = "not found") -> Response:
        return JSONResponse({"error": msg}, status_code=404)

    def parse(request: Request) -> tuple[Library, int] | None:
        lib = cat.libraries.get(request.path_params["library"])
        bid = request.path_params["book_id"]
        return (lib, int(bid)) if lib and bid.isdigit() else None

    @mcp.custom_route("/", methods=["GET"])
    async def index(request: Request) -> Response:
        return JSONResponse({"service": "calibre-mcp", "version": __version__, "mcp": "/mcp",
                             "libraries": {n: f"/{n}/books/<id>" for n in cat.libraries},
                             "convert_to_epub": cat.can_convert})

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True})

    @mcp.custom_route("/{library}/books/{book_id}", methods=["GET"])
    async def book_json(request: Request) -> Response:
        found = parse(request)
        book = found[0].book(found[1]) if found else None
        if not book:
            return not_found("no such book")
        book.update(cat.links(found[0], book, cat.base_url(request.headers)))
        return JSONResponse(book)

    @mcp.custom_route("/{library}/books/{book_id}/{name}", methods=["GET", "HEAD"])
    async def book_file(request: Request) -> Response:
        found = parse(request)
        if not found:
            return not_found("no such book")
        lib, bid = found
        name = request.path_params["name"].lower()
        if name == "cover.jpg":
            path = lib.cover(bid)
            return FileResponse(path, media_type="image/jpeg") if path else not_found("no cover")
        fmt = name.upper()
        book = lib.book(bid)
        if not book:
            return not_found("no such book")
        path = lib.file(bid, fmt) if fmt in book["formats"] else None
        if path is None and fmt == "EPUB":
            source = lib.epub_source(bid, book["formats"])
            if source and source[0] == "original":
                path = lib.file(bid, source[1])
            elif source and cat.can_convert:
                src = lib.file(bid, source[1])
                if src is None:
                    return not_found(f"{source[1]} file is missing on disk")
                try:
                    path = await cat.converter.to_epub(lib.name, bid, src)
                except ConvertError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=502)
            ext = "epub"
        else:
            ext = name
        if path is None and fmt in book["formats"]:
            return not_found(f"the {fmt} file Calibre lists for book {bid} is missing from its folder on disk")
        if path is None:
            return not_found(f"book {bid} has no {fmt} file (has: {', '.join(book['formats']) or 'none'})")
        media = MEDIA.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, headers={
            "Content-Disposition": disposition(lib.file_name(book, ext), f"book-{bid}.{ext}")})

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(cfg.allowed_hosts),
        allowed_hosts=cfg.allowed_hosts, allowed_origins=[f"{p}://{h}" for h in cfg.allowed_hosts
                                                          for p in ("http", "https")])
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                  transport_security=security, host=cfg.host)
    app.state.catalog = cat
    return app

