"""calibre-mcp: MCP tools over Calibre libraries, plus direct HTTP links to every book file.

HTTP (no auth; put it behind a proxy that limits who can reach it):

    GET /                                      libraries and endpoints (JSON)
    GET /health
    GET /<library>/books/<id>                  metadata (JSON)
    GET /<library>/books/<id>/<format>         a book file, e.g. /main/books/1037/epub, /main/books/12/mobi
    GET /<library>/books/<id>/epub             the EPUB, converted on demand when the book has none
    GET /<library>/books/<id>/cover.jpg
    PUT /uploads/<file_id>                     bytes for an upload started with MCP prepare_upload
    POST /mcp                                  MCP (streamable HTTP, stateless)
"""

from __future__ import annotations

import asyncio
import logging
import math
import shlex
import mimetypes
import re
import unicodedata
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from typing import Literal
from urllib.parse import quote, urlsplit

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
from .importer import AddError, BookMetadata, Importer
from .staging import Stager, StagingError
from .library import Library

log = logging.getLogger("calibre_mcp")

INSTRUCTIONS = """\
Tools for Calibre ebook libraries. Find books with search_books (words match title, authors, tags
and series; filters narrow it), inspect one with get_book, and browse authors/tags/series with
browse_library. Every book comes with direct HTTP download URLs for each stored format, an `epub_url`
(converted on demand when the book has no EPUB; the first request can take a minute) and a cover URL.
Book ids are per library; omit `library` to use the default one.
To add a book (when list_libraries reports add_books): call prepare_upload(filename), upload the file's
bytes with the returned method to upload_url (`curl --fail-with-body -T ./book.epub '<upload_url>'`),
then call add_book(file_id, …) with optional metadata overrides. Duplicates and failures before import
keep the upload until expiry, so retry without uploading again. If import succeeds but metadata fails,
the upload is consumed and the error reports book IDs to retry with update_book. Ask the user before using
duplicates=new_record or merge. Use update_book(book_id, …) to change an existing book's metadata;
omitted fields are preserved. Both add_book and update_book accept title, authors, tags, series,
series_index (series number), languages, identifiers, publisher and description."""
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
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


def same_origin(request: Request, allowed_hosts: list[str]) -> bool:
    """Allow scripts and browser requests from the request host or configured hosts."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
        host = parsed.netloc.lower()
    except ValueError:
        return False
    target = (request.headers.get("x-forwarded-host") or request.headers.get("host", "")).split(",")[0].strip()
    return bool(host and parsed.scheme in ("http", "https")
                and (host == target.lower() or host in [h.lower() for h in allowed_hosts]))


def tool_metadata(title: str | None, authors: list[str] | None, tags: list[str] | None,
                  series: str | None, series_index: float | None, languages: list[str] | None,
                  identifiers: dict[str, str] | None, publisher: str | None = None,
                  description: str | None = None, *, updating: bool = False) -> BookMetadata:
    normalized = {}
    for key, value in (identifiers or {}).items():
        key = key.strip().lower()
        if key in normalized:
            raise ToolError("identifier keys must be unique when lowercased")
        normalized[key] = value
    if series_index is not None and not math.isfinite(series_index):
        raise ToolError("series_index must be finite")
    metadata = BookMetadata(title, authors, tags, series, series_index, languages,
                            normalized if identifiers is not None else None, publisher, description)
    try:
        metadata.args(require_series=not updating)
    except AddError as exc:
        raise ToolError(str(exc)) from None
    return metadata


class Catalog:
    """The configured libraries, the converter and URL building."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.libraries = {lib.name: Library(lib.name, lib.path) for lib in cfg.libraries}
        self.default = cfg.libraries[0].name
        self.importer = Importer(cfg.calibredb, timeout=cfg.add_timeout)
        self.max_upload = cfg.max_upload_mb * 1024 * 1024
        self.stager = Stager(cfg.cache_dir / "uploads", ttl=cfg.upload_ttl, max_bytes=self.max_upload,
                             max_entries=cfg.max_staged_uploads)
        log.info("adding books %s (calibredb: %s)", "enabled" if self.can_add else "disabled", cfg.calibredb)
        self.converter = Converter(cfg.ebook_convert, cfg.cache_dir, timeout=cfg.convert_timeout)
        if cfg.convert and not self.converter.available:
            log.warning("ebook-convert not found; books without an EPUB get no epub_url")

    @property
    def can_add(self) -> bool:
        return self.importer.available

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
    cat.stager.sweep()
    mcp = MCPServer("calibre-mcp", instructions=INSTRUCTIONS, version=__version__)

    def lib_or_error(name: str | None) -> Library:
        try:
            return cat.library(name)
        except KeyError as exc:
            raise ToolError(str(exc)) from None

    # -- MCP tools ------------------------------------------------------------------

    @mcp.tool(annotations=READ)
    def list_libraries(ctx: Context) -> dict:
        """The Calibre libraries served here, with book counts and stored formats."""
        return {"default": cat.default, "convert_to_epub": cat.can_convert, "add_books": cat.can_add,
                "update_books": cat.can_add,
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

    @mcp.tool(annotations=WRITE)
    async def prepare_upload(filename: str, ctx: Context) -> dict:
        """Start adding a book: PUT bytes to upload_url, then call add_book with file_id.
        The filename extension picks the format. One file per call; expires after inactivity.
        """
        if not cat.can_add:
            raise ToolError("calibredb is not installed; adding books needs Calibre")
        try:
            staged = cat.stager.prepare(filename)
        except StagingError as exc:
            raise ToolError(str(exc)) from None
        url = f"{cat.base_url(ctx.headers)}/uploads/{staged.file_id}"
        log.info("staged %s (%s)", staged.file_id[:6], staged.filename)
        return {"file_id": staged.file_id, "upload_url": url, "method": "PUT", "filename": staged.filename,
                "max_bytes": cat.max_upload, "expires_at": cat.stager.expires_at(staged),
                "expires_in": cat.stager.expires_in(staged),
                "curl": f"curl --fail-with-body -T <local file> {shlex.quote(url)}"}

    @mcp.tool(annotations=WRITE)
    async def add_book(file_id: str, ctx: Context, library: str | None = None,
                       title: str | None = None, authors: list[str] | None = None, tags: list[str] | None = None,
                       series: str | None = None, series_index: float | None = None,
                       languages: list[str] | None = None, identifiers: dict[str, str] | None = None,
                       duplicates: Literal["refuse", "new_record", "merge"] = "refuse",
                       publisher: str | None = None, description: str | None = None) -> dict:
        """Import a file uploaded via prepare_upload. Metadata overrides the ebook's own.
        series is the series name; series_index is its number. description accepts text or HTML.
        duplicates: refuse returns status duplicate and keeps the upload; new_record creates another
        record; merge adds a missing format. Failures before import keep the upload for retry.
        If import succeeds but publisher/description fails, use the reported IDs with update_book.
        """
        if not cat.can_add:
            raise ToolError("calibredb is not installed; adding books needs Calibre")
        lib = lib_or_error(library)
        metadata = tool_metadata(title, authors, tags, series, series_index, languages, identifiers,
                                 publisher, description)
        try:
            staged = cat.stager.claim(file_id)
        except StagingError as exc:
            raise ToolError(str(exc)) from None
        try:
            result = await cat.importer.add(lib.root, staged.path, metadata, duplicates)
        except AddError as exc:
            if exc.changed:
                cat.stager.consume(file_id)
            else:
                cat.stager.release(file_id)
            raise ToolError(str(exc)) from None
        except BaseException:
            cat.stager.release(file_id)
            raise
        if result.status != "duplicate":
            cat.stager.consume(file_id)
            log.info("consumed %s", file_id[:6])
            books = []
            for bid in result.book_ids:
                book = lib.book(bid)
                if book is not None:
                    books.append(book | cat.links(lib, book, cat.base_url(ctx.headers)))
            if not books:
                raise ToolError("calibredb reported no readable books")
            return {"status": result.status, "library": lib.name, "file_id": file_id, "books": books}
        cat.stager.release(file_id)
        existing = {}
        for duplicate_title in result.duplicate_titles:
            _, books = lib.search(duplicate_title, limit=10)
            for book in books:
                if len(existing) < 10:
                    existing[book["id"]] = book | cat.links(lib, book, cat.base_url(ctx.headers))
        return {"status": "duplicate", "library": lib.name, "file_id": file_id,
                "message": "already in the library: " + "; ".join(result.duplicate_titles)
                + '; the upload is kept — call add_book again with duplicates="new_record" or "merge" '
                'if the user wants that', "existing": list(existing.values()),
                "expires_at": cat.stager.expires_at(staged)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                          idempotent_hint=True, open_world_hint=False))
    async def update_book(book_id: int, ctx: Context, library: str | None = None,
                          title: str | None = None, authors: list[str] | None = None,
                          tags: list[str] | None = None, series: str | None = None,
                          series_index: float | None = None, languages: list[str] | None = None,
                          identifiers: dict[str, str] | None = None, publisher: str | None = None,
                          description: str | None = None) -> dict:
        """Update an existing book's metadata and return its full metadata and download links.
        Omitted/null fields stay unchanged; supplied lists and identifiers replace existing values.
        Empty lists/dictionaries clear those fields; empty series/publisher/description clears them.
        Title must be non-empty. series is the name; series_index is the number and may be set alone.
        description accepts text or HTML. Changes affect library metadata, not embedded file metadata.
        """
        if not cat.can_add:
            raise ToolError("calibredb is not installed; updating books needs Calibre")
        lib = lib_or_error(library)
        if lib.book(book_id) is None:
            raise ToolError(f"no book {book_id} in library {lib.name!r}")
        metadata = tool_metadata(title, authors, tags, series, series_index, languages, identifiers,
                                 publisher, description, updating=True)
        try:
            await cat.importer.update(lib.root, book_id, metadata)
        except AddError as exc:
            raise ToolError(str(exc)) from None
        book = lib.book(book_id)
        if book is None:
            raise ToolError(f"book {book_id} is no longer readable after updating")
        book.update(cat.links(lib, book, cat.base_url(ctx.headers)))
        return book

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
                             "convert_to_epub": cat.can_convert, "add_books": cat.can_add,
                             "upload": "MCP prepare_upload, then PUT /uploads/<file_id>, then MCP add_book"})

    @mcp.custom_route("/uploads/{file_id}", methods=["PUT"])
    async def put_upload(request: Request) -> Response:
        if not same_origin(request, cfg.allowed_hosts):
            return JSONResponse({"error": "cross-origin upload refused"}, status_code=403)
        if not cat.can_add:
            return JSONResponse({"error": "calibredb is not installed; adding books needs Calibre"}, status_code=503)
        try:
            staged = await cat.stager.receive(request.path_params["file_id"], request, cat.max_upload)
        except StagingError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        log.info("received %s (%s bytes)", staged.file_id[:6], staged.size)
        return JSONResponse({"file_id": staged.file_id, "filename": staged.filename, "size": staged.size,
                             "sha256": staged.sha256, "expires_at": cat.stager.expires_at(staged)})

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
    original_lifespan = app.router.lifespan_context

    async def reaper():
        while True:
            await asyncio.sleep(min(60, cfg.upload_ttl))
            try:
                count = cat.stager.reap()
                log.debug("reaped %s uploads", count)
            except Exception:
                log.exception("upload reaper failed")

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            task = asyncio.create_task(reaper())
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app.router.lifespan_context = lifespan
    app.state.catalog = cat
    return app

