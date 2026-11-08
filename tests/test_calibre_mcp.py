"""Smoke tests against a small synthetic Calibre library: python -m unittest discover -s tests"""

from __future__ import annotations

import asyncio
import json
import io
import sys
from contextlib import redirect_stdout
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

from calibre_mcp.config import Config, LibraryConfig
from calibre_mcp.server import build_app

SCHEMA = """
CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT, timestamp TEXT, pubdate TEXT, series_index REAL,
  author_sort TEXT, path TEXT, uuid TEXT, has_cover INTEGER, last_modified TEXT);
CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, uncompressed_size INTEGER, name TEXT);
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT);
CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, link TEXT);
CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT);
CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT);
CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER);
CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT, link TEXT);
CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER, lang_code INTEGER, item_order INTEGER);
CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER, link TEXT);
CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER, rating INTEGER);
CREATE TABLE comments (id INTEGER PRIMARY KEY, book INTEGER, text TEXT);
CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT);
"""


def make_library(root: Path) -> None:
    root.mkdir(parents=True)
    db = sqlite3.connect(root / "metadata.db")
    db.executescript(SCHEMA)
    books = [
        (1, "On Food and Cooking", "Harold McGee", "EPUB", "2026-10-17", 1),
        (2, "鸡蛋的科学", "张三", "MOBI", "2026-10-18", 0),
        (3, "Odd Comic", "Some Artist", "ZIP", "2026-10-19", 0),
    ]
    for bid, title, author, fmt, added, cover in books:
        rel = f"{author}/{title} ({bid})"
        (root / rel).mkdir(parents=True)
        name = f"{title} - {author}"
        (root / rel / f"{name}.{fmt.lower()}").write_bytes(f"{fmt} bytes of {title}".encode())
        if cover:
            (root / rel / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
        db.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (bid, title, title, added + " 00:00:00+00:00", "0101-01-01 00:00:00+00:00", 1.0, author, rel,
                    f"uuid-{bid}", cover, added))
        db.execute("INSERT INTO data VALUES (NULL,?,?,?,?)", (bid, fmt, 20, name))
        db.execute("INSERT INTO authors VALUES (?,?,?,'')", (bid, author, author))
        db.execute("INSERT INTO books_authors_link VALUES (NULL,?,?)", (bid, bid))
        db.execute("INSERT INTO languages VALUES (?,?,'')", (bid, "zho" if bid == 2 else "eng"))
        db.execute("INSERT INTO books_languages_link VALUES (NULL,?,?,0)", (bid, bid))
    db.execute("INSERT INTO tags VALUES (1,'Cooking','')")
    db.execute("INSERT INTO books_tags_link VALUES (NULL,1,1)")
    db.execute("INSERT INTO books_tags_link VALUES (NULL,2,1)")
    db.execute("INSERT INTO series VALUES (1,'Kitchen Science','Kitchen Science','')")
    db.execute("INSERT INTO books_series_link VALUES (NULL,1,1)")
    db.execute("INSERT INTO comments VALUES (NULL,1,'<p>The classic.</p><p>Second &amp; last.</p>')")
    db.execute("INSERT INTO identifiers VALUES (NULL,1,'isbn','9780684800011')")
    db.commit()
    db.close()


def fake_converter(path: Path) -> Path:
    path.write_text("#!/bin/sh\nprintf 'converted:' > \"$2\"\ncat \"$1\" >> \"$2\"\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class CalibreMcpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp())
        make_library(cls.tmp / "lib")
        cls.cfg = Config(libraries=[LibraryConfig("main", cls.tmp / "lib")], cache_dir=cls.tmp / "cache",
                         ebook_convert=str(fake_converter(cls.tmp / "ebook-convert")))

    def setUp(self) -> None:
        self.app = build_app(self.cfg)  # an MCP session manager runs once per app

    def test_http(self) -> None:
        with TestClient(self.app, base_url="http://calibre.test") as c:
            self.assertEqual(c.get("/").json()["libraries"], {"main": "/main/books/<id>"})
            meta = c.get("/main/books/1").json()
            self.assertEqual(meta["epub_url"], "http://calibre.test/main/books/1/epub")
            self.assertEqual(meta["description"], "The classic.\nSecond & last.")
            r = c.get("/main/books/1/epub")
            self.assertEqual((r.status_code, r.content), (200, b"EPUB bytes of On Food and Cooking"))
            self.assertIn('filename="On Food and Cooking - Harold McGee.epub"', r.headers["content-disposition"])
            self.assertIn("On Food and Cooking - Harold McGee.epub", unquote(r.headers["content-disposition"]))
            self.assertEqual(c.get("/main/books/1/cover.jpg").status_code, 200)
            self.assertEqual(c.get("/main/books/2/cover.jpg").status_code, 404)
            r = c.get("/main/books/2/mobi")
            self.assertEqual(r.content, "MOBI bytes of 鸡蛋的科学".encode())
            self.assertIn('filename="book-2.mobi"', r.headers["content-disposition"])
            self.assertIn("鸡蛋的科学 - 张三.mobi", unquote(r.headers["content-disposition"]))
            r = c.get("/main/books/2/epub")  # converted on demand
            self.assertEqual(r.content, "converted:MOBI bytes of 鸡蛋的科学".encode())
            self.assertEqual(len(list((self.tmp / "cache" / "main").glob("2-*.epub"))), 1)
            self.assertEqual(c.get("/main/books/3/epub").status_code, 404)  # ZIP: not convertible
            self.assertEqual(c.get("/main/books/9/epub").status_code, 404)
            self.assertEqual(c.get("/nope/books/1/epub").status_code, 404)
            self.assertEqual(c.get("/main/books/1/pdf").status_code, 404)
            gone = self.tmp / "lib" / "Some Artist" / "Odd Comic (3)" / "Odd Comic - Some Artist.zip"
            gone.rename(gone.with_suffix(".bak"))
            r = c.get("/main/books/3/zip")
            self.assertIn("missing from its folder", r.json()["error"])
            gone.with_suffix(".bak").rename(gone)
            r = c.get("/main/books/1", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "books.lan"})
            self.assertTrue(r.json()["downloads"]["EPUB"].startswith("https://books.lan/main/books/1/"))

    def test_mcp(self) -> None:
        from mcp import Client

        async def call(client, name, **args):
            res = await client.call_tool(name, args)
            text = " ".join(getattr(c, "text", "") for c in res.content)
            return ("error", text) if res.is_error else json.loads(text)

        async def main() -> None:
            import httpx2
            from mcp.client.streamable_http import streamable_http_client

            transport = httpx2.ASGITransport(app=self.app) if hasattr(httpx2, "ASGITransport") else None
            if transport is None:
                self.skipTest("httpx2 has no ASGITransport")
            async with self.app.router.lifespan_context(self.app):
                http = httpx2.AsyncClient(transport=transport, base_url="http://calibre.test")
                async with Client(streamable_http_client("http://calibre.test/mcp", http_client=http)) as client:
                    names = {t.name for t in (await client.list_tools()).tools}
                    self.assertEqual(names, {"list_libraries", "search_books", "get_book", "browse_library", "prepare_upload", "add_book"})
                    libs = await call(client, "list_libraries")
                    self.assertEqual(libs["libraries"][0]["books"], 3)
                    self.assertEqual(libs["add_books"], self.app.state.catalog.can_add)
                    self.assertNotIn("add_url", libs["libraries"][0])
                    res = await call(client, "search_books", query="cooking")
                    self.assertEqual([b["id"] for b in res["books"]], [1, 2])  # title hit first, tag hit second
                    self.assertEqual(res["books"][0]["epub_url"], "http://calibre.test/main/books/1/epub")
                    res = await call(client, "search_books", language="zho")
                    self.assertEqual(res["books"][0]["epub_from"], "converted from MOBI")
                    res = await call(client, "search_books", author="mcgee", format="epub")
                    self.assertEqual(res["total"], 1)
                    res = await call(client, "search_books")
                    self.assertEqual([b["id"] for b in res["books"]], [3, 2, 1])  # newest first
                    self.assertNotIn("epub_url", res["books"][0])
                    book = await call(client, "get_book", book_id=1)
                    self.assertEqual(book["identifiers"], {"isbn": "9780684800011"})
                    self.assertEqual(book["series"], "Kitchen Science")
                    self.assertEqual((await call(client, "get_book", book_id=99))[0], "error")
                    self.assertEqual((await call(client, "get_book", book_id=1, library="x"))[0], "error")
                    tags = await call(client, "browse_library", kind="tags")
                    self.assertEqual(tags["items"], [{"name": "Cooking", "books": 2}])

        asyncio.run(main())





def fake_calibredb(path: Path) -> Path:
    path.write_text(f"#!{sys.executable}\n" + r'''import json
import pathlib
import shutil
import sqlite3
import sys
args = sys.argv[1:]
assert args[0] == "add"
root = pathlib.Path(args[args.index("--with-library") + 1])
src = pathlib.Path(args[args.index("--") + 1])
(root / "last-args.json").write_text(json.dumps(args))
def option(key, default):
    return args[args.index(key) + 1] if key in args else default
title = option("--title", src.stem)
author = option("--authors", "Unknown")
db = sqlite3.connect(root / "metadata.db")
old = db.execute("SELECT id FROM books WHERE title=? AND author_sort=?", (title, author)).fetchone()
if old and "--duplicates" not in args:
    if "--automerge" in args and not db.execute("SELECT 1 FROM data WHERE book=? AND format=?",
                                               (old[0], src.suffix[1:].upper())).fetchone():
        bid = old[0]
        status = "Merged"
    else:
        print("The following books were not added as they already exist in the database:")
        print("  " + title)
        sys.exit(0)
else:
    bid = db.execute("SELECT MAX(id)+1 FROM books").fetchone()[0]
    rel = f"{author}/{title} ({bid})"
    db.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (bid, title, title, "2026-10-20", "0101-01-01", 1.0, author, rel, f"uuid-{bid}", 0, "2026-10-20"))
    aid = db.execute("INSERT INTO authors VALUES (NULL,?,?, '')", (author, author)).lastrowid
    db.execute("INSERT INTO books_authors_link VALUES (NULL,?,?)", (bid, aid))
    status = "Added"
rel = db.execute("SELECT path FROM books WHERE id=?", (bid,)).fetchone()[0]
dest = root / rel
dest.mkdir(parents=True, exist_ok=True)
name = f"{title} - {author}"
shutil.copyfile(src, dest / (name + src.suffix))
db.execute("INSERT INTO data VALUES (NULL,?,?,?,?)", (bid, src.suffix[1:].upper(), src.stat().st_size, name))
db.commit()
print(f"{status} book ids: {bid}")
''')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class AddBookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        make_library(self.root / "lib")
        self.cfg = Config(libraries=[LibraryConfig("main", self.root / "lib")], cache_dir=self.root / "cache",
                          convert=False, calibredb=str(fake_calibredb(self.root / "calibredb")))
        self.app = build_app(self.cfg)

    def run_mcp(self, scenario):
        from mcp import Client
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        async def run():
            async with self.app.router.lifespan_context(self.app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),
                                              base_url="http://books.test") as http:
                    async with Client(streamable_http_client("http://books.test/mcp", http_client=http)) as client:
                        await scenario(client, http)
        asyncio.run(run())

    async def call(self, client, name, error=None, **args):
        result = await client.call_tool(name, args)
        text = " ".join(getattr(item, "text", "") for item in result.content)
        if error is not None:
            self.assertTrue(result.is_error, text)
            self.assertIn(error, text)
            return text
        self.assertFalse(result.is_error, text)
        return json.loads(text)

    async def stage(self, client, http, name="x.epub", body=b"book bytes"):
        up = await self.call(client, "prepare_upload", filename=name)
        response = await http.put(up["upload_url"], content=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["size"], len(body))
        return up

    def test_add_and_duplicates(self):
        async def run(client, http):
            tools = (await client.list_tools()).tools
            self.assertEqual({t.name for t in tools}, {"list_libraries", "search_books", "get_book",
                             "browse_library", "prepare_upload", "add_book"})
            for tool in tools:
                self.assertEqual(tool.annotations.read_only_hint, tool.name not in ("prepare_upload", "add_book"))
            libs = await self.call(client, "list_libraries")
            self.assertTrue(libs["add_books"])
            self.assertNotIn("add_url", libs["libraries"][0])
            up = await self.stage(client, http, "../My Book.epub")
            self.assertEqual(up["filename"], "My Book.epub")
            self.assertEqual(up["method"], "PUT")
            self.assertTrue(up["expires_at"].endswith("Z"))
            self.assertIn("curl --fail-with-body -T", up["curl"])
            metadata = dict(title="New Book", authors=["A", "B"], tags=["one", "two"], languages=["eng", "zho"],
                            series="S", series_index=2.5, identifiers={" ISBN ": "123"})
            result = await self.call(client, "add_book", file_id=up["file_id"], **metadata)
            self.assertEqual(result["status"], "added")
            self.assertEqual((await http.get(result["books"][0]["downloads"]["EPUB"])).content, b"book bytes")
            self.assertFalse((self.cfg.cache_dir / "uploads" / (".staged-" + up["file_id"])).exists())
            await self.call(client, "add_book", error="unknown or expired file_id", file_id=up["file_id"])
            self.assertEqual((await http.put(up["upload_url"], content=b"x")).status_code, 404)
            args = json.loads((self.root / "lib" / "last-args.json").read_text())
            for flag, value in (("--authors", "A & B"), ("--tags", "one,two"), ("--languages", "eng,zho"),
                                ("--series", "S"), ("--series-index", "2.5"), ("--identifier", "isbn:123")):
                self.assertEqual(args[args.index(flag) + 1], value)
            up = await self.stage(client, http)
            result = await self.call(client, "add_book", file_id=up["file_id"], **metadata)
            self.assertEqual(result["status"], "duplicate")
            self.assertEqual([b["id"] for b in result["existing"]], [4])
            result = await self.call(client, "add_book", file_id=up["file_id"], duplicates="new_record", **metadata)
            self.assertEqual(result["status"], "added")
            up = await self.stage(client, http, "x.mobi")
            self.assertEqual((await self.call(client, "add_book", file_id=up["file_id"], duplicates="merge",
                                             **metadata))["status"], "merged")
            up = await self.stage(client, http)
            self.assertEqual((await self.call(client, "add_book", file_id=up["file_id"], duplicates="merge",
                                             **metadata))["status"], "duplicate")
            result = await self.call(client, "search_books", query="New Book")
            self.assertEqual(result["total"], 2)
        self.run_mcp(run)

    def test_validation_and_http_guards(self):
        async def run(client, http):
            cat = self.app.state.catalog
            await self.call(client, "prepare_upload", filename=".epub", error="non-empty stem")
            self.assertEqual(len(cat.stager), 0)
            up = await self.call(client, "prepare_upload", filename="x.epub")
            await self.call(client, "add_book", file_id=up["file_id"], error="no bytes uploaded yet")
            self.assertEqual((await http.put(up["upload_url"], content=b"x",
                                            headers={"Origin": "https://foreign.test"})).status_code, 403)
            self.assertEqual((await http.put(up["upload_url"], content=b"x", headers={
                "Origin": "https://proxy.test", "X-Forwarded-Host": "proxy.test"})).status_code, 200)
            for metadata, message in (({"title": " "}, "title must not be empty"),
                    ({"series_index": 2}, "series_index needs series"),
                    ({"identifiers": {"ISBN": "1", "isbn": "2"}}, "unique when lowercased"),
                    ({"library": "missing"}, "no library"), ({"duplicates": "bad"}, "duplicates")):
                await self.call(client, "add_book", file_id=up["file_id"], error=message, **metadata)
            self.assertFalse((self.root / "lib" / "last-args.json").exists())
            cat.max_upload = 2
            self.assertEqual((await http.put(up["upload_url"], content=b"123")).status_code, 413)
            self.assertEqual((await http.put(up["upload_url"], content=b"")).status_code, 400)
            self.assertEqual((await http.get(up["upload_url"])).status_code, 405)
            self.assertEqual((await http.put("/uploads/bad", content=b"x")).status_code, 404)
            self.assertGreaterEqual((await http.post("/main/books", content=b"x")).status_code, 400)
            self.assertIn("prepare_upload", (await http.get("/")).json()["upload"])
            cat.stager.max_entries = 1
            await self.call(client, "prepare_upload", filename="x.epub", error="too many pending uploads")
            await self.call(client, "add_book", file_id=up["file_id"])
            cat.importer._available = False
            await self.call(client, "prepare_upload", filename="x.epub", error="calibredb is not installed")
            await self.call(client, "add_book", file_id="x", error="calibredb is not installed")
            self.assertEqual((await http.put(up["upload_url"], content=b"x")).status_code, 503)
            self.assertFalse((await self.call(client, "list_libraries"))["add_books"])
        self.run_mcp(run)

    def test_import_errors_and_missing_ids(self):
        from calibre_mcp.importer import AddError, AddResult

        async def run(client, http):
            cat = self.app.state.catalog
            up = await self.stage(client, http)
            for error in (AddError("library is locked", 503), AddError("calibredb timed out", 502),
                          AddError("calibredb failed: stderr tail", 502)):
                before = cat.stager._entries[up["file_id"]].expires
                with patch.object(cat.importer, "add", AsyncMock(side_effect=error)):
                    await self.call(client, "add_book", file_id=up["file_id"], error=str(error))
                self.assertGreaterEqual(cat.stager._entries[up["file_id"]].expires, before)
                self.assertTrue(cat.stager._entries[up["file_id"]].path.exists())
            await self.call(client, "add_book", file_id=up["file_id"])
            for ids in ([999], [999, 1]):
                up = await self.stage(client, http)
                with patch.object(cat.importer, "add", AsyncMock(return_value=AddResult("added", ids, []))):
                    if ids == [999]:
                        await self.call(client, "add_book", file_id=up["file_id"], error="no readable books")
                    else:
                        result = await self.call(client, "add_book", file_id=up["file_id"])
                        self.assertEqual([b["id"] for b in result["books"]], [1])
                self.assertNotIn(up["file_id"], cat.stager._entries)
        self.run_mcp(run)

    def test_import_diagnostics(self):
        async def run(client, http):
            up = await self.stage(client, http)
            proc = subprocess.CompletedProcess("calibredb", 0, stdout="", stderr=(
                "omit\na\nb\nc\nd\nreader failed: useful diagnostic\n"))
            with patch("calibre_mcp.importer.subprocess.run", return_value=proc):
                await self.call(client, "add_book", file_id=up["file_id"], error=
                                "calibredb added nothing: a | b | c | d | reader failed: useful diagnostic")
            timeout = subprocess.TimeoutExpired("calibredb", self.cfg.add_timeout, output=b"ignored", stderr=(
                b"omit\na\nb\nc\nd\nreader stalled: useful diagnostic \xff\n"))
            with patch("calibre_mcp.importer.subprocess.run", side_effect=timeout):
                await self.call(client, "add_book", file_id=up["file_id"], error=
                                f"calibredb timed out after {self.cfg.add_timeout}s: "
                                "a | b | c | d | reader stalled: useful diagnostic \ufffd")
            await self.call(client, "add_book", file_id=up["file_id"])
        self.run_mcp(run)

    def test_concurrency(self):
        from calibre_mcp.importer import AddResult

        async def run(client, http):
            cat = self.app.state.catalog
            up = await self.stage(client, http)
            started, finish = asyncio.Event(), asyncio.Event()

            async def slow(*args):
                started.set()
                await finish.wait()
                return AddResult("added", [1], [])

            with patch.object(cat.importer, "add", AsyncMock(side_effect=slow)) as importer:
                first = asyncio.create_task(self.call(client, "add_book", file_id=up["file_id"]))
                await started.wait()
                await self.call(client, "add_book", file_id=up["file_id"], error="file_id is busy")
                self.assertEqual((await http.put(up["upload_url"], content=b"x")).status_code, 409)
                finish.set()
                await first
                self.assertEqual(importer.await_count, 1)
        self.run_mcp(run)

    def test_lifespan_reaper(self):
        cat = self.app.state.catalog
        staged = cat.stager.prepare("expired.epub")
        staged.expires = cat.stager.clock() - 1
        # Shorten only the sleep interval after build_app has validated the production limit.
        self.cfg.upload_ttl = 0.01

        async def run():
            reaped = asyncio.Event()
            original = cat.stager.reap

            def reap():
                count = original()
                if count:
                    reaped.set()
                return count

            with patch.object(cat.stager, "reap", side_effect=reap):
                async with self.app.router.lifespan_context(self.app):
                    await asyncio.wait_for(reaped.wait(), 2)
            self.assertFalse(staged.dir.exists())
            self.assertEqual(len(cat.stager), 0)
        asyncio.run(run())

    def test_allowed_origin_startup_sweep_and_check(self):
        self.cfg.allowed_hosts = ["other.test", "books.test"]
        for prefix in (".upload-", ".staged-"):
            leftover = self.cfg.cache_dir / "uploads" / (prefix + "old")
            leftover.mkdir(parents=True)
            (leftover / "x.epub").write_bytes(b"leftover")
        self.app = build_app(self.cfg)
        self.assertEqual(list((self.cfg.cache_dir / "uploads").iterdir()), [])

        async def run(client, http):
            up = await self.call(client, "prepare_upload", filename="x.epub")
            self.assertEqual((await http.put(up["upload_url"], content=b"x", headers={
                "Origin": "https://other.test"})).status_code, 200)
        self.run_mcp(run)
        from calibre_mcp.__main__ import main
        out = io.StringIO()
        with redirect_stdout(out):
            main(["check", "-l", f"main={self.root / 'lib'}", "--calibredb", str(self.root / "calibredb"),
                  "--max-upload-mb", "7", "--upload-ttl", "120", "--max-staged-uploads", "3"])
        self.assertIn(f"add books: yes (calibredb: {self.root / 'calibredb'}; max upload: 7 MB; "
                      "upload ttl: 120 s; max staged: 3)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
