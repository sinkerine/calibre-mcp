"""Smoke tests against a small synthetic Calibre library: python -m unittest discover -s tests"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

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


if __name__ == "__main__":
    unittest.main()
