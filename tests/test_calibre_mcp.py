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
                    self.assertEqual(names, {"list_libraries", "search_books", "get_book", "browse_library"})
                    libs = await call(client, "list_libraries")
                    self.assertEqual(libs["libraries"][0]["books"], 3)
                    self.assertEqual(libs["add_books"], self.app.state.catalog.can_add)
                    if libs["add_books"]:
                        self.assertEqual(libs["libraries"][0]["add_url"], "http://calibre.test/main/books")
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

    def test_add_and_duplicates(self):
        with TestClient(self.app, base_url="http://books.test") as c:
            self.assertTrue(c.get("/").json()["add_books"])
            r = c.post("/main/books", files={"file": ("x.epub", b"book bytes")},
                       data={"title": "New Book", "authors": "A & B", "tags": "one,two", "languages": "eng,zho",
                             "series": "S", "series_index": "2.5", "identifier": "isbn:123"})
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual(r.headers["location"], "http://books.test/main/books/4")
            book = r.json()["books"][0]
            self.assertEqual(book["title"], "New Book")
            self.assertEqual(c.get(book["downloads"]["EPUB"]).content, b"book bytes")
            self.assertEqual(self.app.state.catalog.library("main").search("New Book")[0], 1)
            args = json.loads((self.root / "lib" / "last-args.json").read_text())
            for flag, value in (("--authors", "A & B"), ("--tags", "one,two"), ("--languages", "eng,zho"),
                                ("--series", "S"), ("--series-index", "2.5"), ("--identifier", "isbn:123")):
                self.assertEqual(args[args.index(flag) + 1], value)
            url = "/main/books?filename=x.epub&title=New%20Book&authors=A%20%26%20B"
            r = c.post(url, content=b"again")
            self.assertEqual(r.status_code, 409, r.text)
            self.assertEqual([b["id"] for b in r.json()["existing"]], [4])
            r = c.post(url + "&duplicates=new_record", content=b"again")
            self.assertEqual(r.status_code, 201, r.text)
            r = c.post(url.replace("x.epub", "x.mobi") + "&duplicates=merge", content=b"mobi")
            self.assertEqual((r.status_code, r.json()["status"]), (200, "merged"))
            self.assertEqual(c.post(url + "&duplicates=merge", content=b"again").status_code, 409)
            self.assertEqual(list((self.cfg.cache_dir / "uploads").iterdir()), [])

    def test_raw_metadata_and_errors(self):
        with TestClient(self.app, base_url="http://books.test") as c:
            r = c.post("/main/books?filename=raw.epub&title=Raw&authors=Writer&tags=a,b&identifier=isbn:9",
                       content=b"raw", headers={"Content-Type": "application/x-www-form-urlencoded",
                                                "Origin": "http://books.test"})
            self.assertEqual(r.status_code, 201, r.text)
            args = json.loads((self.root / "lib" / "last-args.json").read_text())
            self.assertEqual(args[args.index("--title") + 1], "Raw")
            calls = (self.root / "lib" / "last-args.json").read_bytes()
            for query in ("duplicates=bad", "series_index=no", "title=", "identifier=bad"):
                self.assertEqual(c.post("/main/books?filename=x.epub&" + query, content=b"x").status_code, 400)
                self.assertEqual((self.root / "lib" / "last-args.json").read_bytes(), calls)
            self.assertEqual(c.post("/nope/books", content=b"x").status_code, 404)
            self.assertEqual(c.post("/main/books?filename=x.epub", content=b"x",
                                    headers={"Origin": "https://foreign.test"}).status_code, 403)
            self.assertEqual(c.post("/main/books?filename=forward.epub", content=b"x", headers={
                "Origin": "https://proxy.test", "X-Forwarded-Host": "proxy.test"}).status_code, 201)
            self.app.state.catalog.max_upload = 2
            self.assertEqual(c.post("/main/books?filename=x.epub", content=b"123").status_code, 413)
            self.assertEqual(list((self.cfg.cache_dir / "uploads").iterdir()), [])

    def test_unavailable_and_check(self):
        self.cfg.calibredb = str(self.root / "missing-calibredb")
        with TestClient(build_app(self.cfg)) as c:
            self.assertFalse(c.get("/").json()["add_books"])
            r = c.post("/main/books", content=b"")
            self.assertEqual(r.status_code, 503)
            self.assertIn("calibredb is not installed", r.json()["error"])
        from calibre_mcp.__main__ import main
        out = io.StringIO()
        with redirect_stdout(out):
            main(["check", "-l", f"main={self.root / 'lib'}", "--calibredb", str(self.root / "calibredb"),
                  "--max-upload-mb", "7"])
        self.assertIn(f"add books: yes (calibredb: {self.root / 'calibredb'}; max upload: 7 MB)", out.getvalue())

    def test_import_errors_and_missing_ids(self):
        from calibre_mcp.importer import AddError, AddResult

        with TestClient(self.app) as c:
            for error in (AddError("library is locked", 503), AddError("calibredb timed out", 502),
                          AddError("calibredb failed: stderr tail", 502)):
                with patch.object(self.app.state.catalog.importer, "add", AsyncMock(side_effect=error)):
                    r = c.post("/main/books?filename=x.epub", content=b"x")
                    self.assertEqual((r.status_code, r.json()), (error.status, {"error": str(error)}))
                    self.assertEqual(list((self.cfg.cache_dir / "uploads").iterdir()), [])
            with patch.object(self.app.state.catalog.importer, "add",
                              AsyncMock(return_value=AddResult("added", [999], []))):
                self.assertEqual(c.post("/main/books?filename=x.epub", content=b"x").status_code, 502)
            with patch.object(self.app.state.catalog.importer, "add",
                              AsyncMock(return_value=AddResult("added", [999, 1], []))):
                r = c.post("/main/books?filename=x.epub", content=b"x")
                self.assertEqual(r.status_code, 201)
                self.assertEqual([book["id"] for book in r.json()["books"]], [1])

    def test_unrecognized_import_output_retains_stderr_diagnostic(self):
        proc = subprocess.CompletedProcess("calibredb", 0, stdout="", stderr=(
            "omit\na\nb\nc\nd\nreader failed: useful diagnostic\n"))
        with TestClient(self.app) as c, patch("calibre_mcp.importer.subprocess.run", return_value=proc):
            r = c.post("/main/books?filename=x.epub", content=b"x")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json(), {"error":
                         "calibredb added nothing: a | b | c | d | reader failed: useful diagnostic"})
        self.assertEqual(list((self.cfg.cache_dir / "uploads").iterdir()), [])

    def test_import_timeout_retains_stderr_diagnostic(self):
        timeout = subprocess.TimeoutExpired("calibredb", self.cfg.add_timeout, output=b"ignored stdout", stderr=(
            b"omit\na\nb\nc\nd\nreader stalled: useful diagnostic \xff\n"))
        with TestClient(self.app) as c, patch("calibre_mcp.importer.subprocess.run", side_effect=timeout):
            r = c.post("/main/books?filename=x.epub", content=b"x")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json(), {"error": f"calibredb timed out after {self.cfg.add_timeout}s: "
                         "a | b | c | d | reader stalled: useful diagnostic \ufffd"})
        self.assertEqual(list((self.cfg.cache_dir / "uploads").iterdir()), [])

    def test_discovery_and_search_after_upload(self):
        from mcp import Client
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        async def run():
            if not hasattr(httpx2, "ASGITransport"):
                self.skipTest("httpx2 has no ASGITransport")
            async with self.app.router.lifespan_context(self.app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),
                                              base_url="http://books.test") as http:
                    async with Client(streamable_http_client("http://books.test/mcp", http_client=http)) as client:
                        self.assertEqual({t.name for t in (await client.list_tools()).tools},
                                         {"list_libraries", "search_books", "get_book", "browse_library"})
                        res = await client.call_tool("list_libraries", {})
                        libs = json.loads(" ".join(getattr(item, "text", "") for item in res.content))
                        self.assertTrue(libs["add_books"])
                        self.assertEqual(libs["libraries"][0]["add_url"], "http://books.test/main/books")
                        r = await http.post(libs["libraries"][0]["add_url"] + "?filename=Discover.epub",
                                            content=b"new book")
                        self.assertEqual(r.status_code, 201)
                        res = await client.call_tool("search_books", {"query": "Discover"})
                        books = json.loads(" ".join(getattr(item, "text", "") for item in res.content))
                        self.assertEqual([b["title"] for b in books["books"]], ["Discover"])
                        self.app.state.catalog.importer._available = False
                        res = await client.call_tool("list_libraries", {})
                        libs = json.loads(" ".join(getattr(item, "text", "") for item in res.content))
                        self.assertFalse(libs["add_books"])
                        self.assertNotIn("add_url", libs["libraries"][0])

        asyncio.run(run())

    def test_allowed_origin_and_startup_sweep(self):
        self.cfg.allowed_hosts = ["other.test"]
        leftover = self.cfg.cache_dir / "uploads" / ".upload-old"
        leftover.mkdir(parents=True)
        (leftover / "x.epub").write_bytes(b"leftover")
        app = build_app(self.cfg)
        self.assertFalse(leftover.exists())
        with TestClient(app) as c:
            self.assertEqual(c.post("/main/books?filename=x.epub", content=b"x",
                                    headers={"Origin": "https://other.test"}).status_code, 201)


if __name__ == "__main__":
    unittest.main()
