"""Upload parsing, limits and temporary-file lifecycle."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
import starlette.formparsers

from calibre_mcp.upload import UploadError, metadata_from, receive, safe_filename, sweep


class UploadTest(unittest.TestCase):
    def test_safe_filename(self):
        for name in ("../../book.epub", r"C:\books\book.epub", " .book.epub. ", "bo\x00ok.epub"):
            self.assertEqual(safe_filename(name), "book.epub")
        self.assertEqual(safe_filename("鸡蛋.epub"), "鸡蛋.epub")
        self.assertEqual(safe_filename("a" * 200 + ".epub"), "a" * 150 + ".epub")
        for name in ("..", "book", ".epub", "a/", "a\\", "a.*?"):
            with self.subTest(name=name), self.assertRaises(UploadError):
                safe_filename(name)

    def test_metadata_from(self):
        meta, dup = metadata_from({"title": ["Book"], "authors": ["A & B", "C"], "tags": ["x,y", "z"],
                                   "languages": ["eng,zho"], "series": ["S"], "series_index": ["2.5"],
                                   "identifier": ["ISBN:123", "asin:456"], "unknown": ["ignored"]})
        self.assertEqual(meta.authors, ["A", "B", "C"])
        self.assertEqual(meta.tags, ["x", "y", "z"])
        self.assertEqual(meta.languages, ["eng", "zho"])
        self.assertEqual(meta.identifiers, {"isbn": "123", "asin": "456"})
        self.assertEqual((meta.series_index, dup), (2.5, "refuse"))
        for fields in ({"series_index": ["bad"]}, {"series_index": ["nan"]}, {"series_index": ["2"]},
                       {"duplicates": ["bad"]}, {"duplicates": [""]}, {"title": ["a", "b"]}, {"title": [""]},
                       {"identifier": ["bad"]}, {"identifier": ["isbn:x", "ISBN:y"]}):
            with self.subTest(fields=fields), self.assertRaises(UploadError):
                metadata_from(fields)

    def test_receive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "uploads"

            async def endpoint(request):
                try:
                    async with receive(request, root, 1024) as up:
                        self.assertTrue(up.path.exists())
                        return JSONResponse({"name": up.path.name, "body": up.path.read_text(),
                                             "title": up.metadata.title})
                except UploadError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=exc.status)

            with TestClient(Starlette(routes=[Route("/", endpoint, methods=["POST"])])) as c:
                r = c.post("/", files={"file": ("../x.epub", b"hello")}, data={"title": "Title"})
                self.assertEqual((r.status_code, r.json()),
                                 (200, {"name": "x.epub", "body": "hello", "title": "Title"}))
                self.assertEqual(list(root.iterdir()), [])
                r = c.post("/", files={"file": ("x.epub", b"hello")}, data={"filename": "override.epub"})
                self.assertEqual(r.json()["name"], "override.epub")
                self.assertEqual(c.post("/?filename=x.epub", content=b"raw").json()["body"], "raw")
                self.assertEqual(c.post("/", content=b"raw", headers={
                    "Content-Disposition": 'attachment; filename="x.epub"'}).status_code, 200)
                cases = [({}, 400), ({"content": b"", "params": {"filename": "x.epub"}}, 400),
                         ({"content": b"x"}, 400),
                         ({"files": {"other": ("x.epub", b"x")}}, 400),
                         ({"files": [("file", ("x.epub", b"x")), ("file", ("y.epub", b"y"))]}, 400),
                         ({"files": {"file": ("x", b"x")}}, 400),
                         ({"files": {"file": ("x.epub", b"")}}, 400),
                         ({"content": b"x", "headers": {"Content-Length": "2048"}}, 413),
                         ({"content": iter([b"a" * 600, b"b" * 600]), "params": {"filename": "x.epub"}}, 413),
                         ({"files": {"file": ("x.epub", b"a" * 950)}}, 413),
                         ({"files": {"file": ("x.epub", b"x")}, "data": {"duplicates": "bad"}}, 400)]
                for kwargs, status in cases:
                    with self.subTest(kwargs=kwargs):
                        self.assertEqual(c.post("/", **kwargs).status_code, status)
                        self.assertEqual(list(root.iterdir()), [])

    def test_multipart_spools_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spools = []
            original = starlette.formparsers.SpooledTemporaryFile

            def track(*args, **kwargs):
                spool = original(*args, **kwargs)
                spools.append(spool)
                return spool

            async def endpoint(request):
                try:
                    async with receive(request, root, 300):
                        pass
                except UploadError as exc:
                    return JSONResponse({}, status_code=exc.status)
                return JSONResponse({})

            head = b'--b\r\nContent-Disposition: form-data; name="file"; filename="x.epub"\r\n\r\n'
            with patch("starlette.formparsers.SpooledTemporaryFile", track):
                with TestClient(Starlette(routes=[Route("/", endpoint, methods=["POST"])])) as c:
                    # Direct ASGI stream ensures the spool opens before the next chunk exceeds the cap.
                    import asyncio
                    from starlette.requests import Request

                    chunks = iter([head + b"x", b"y" * 301])

                    async def body():
                        return {"type": "http.request", "body": next(chunks), "more_body": True}

                    request = Request({"type": "http", "method": "POST", "path": "/", "query_string": b"",
                                       "headers": [(b"content-type", b"multipart/form-data; boundary=b")]}, body)
                    self.assertEqual(asyncio.run(endpoint(request)).status_code, 413)
                    self.assertTrue(spools)
                    self.assertTrue(all(spool.closed for spool in spools))
                    r = c.post("/", files={"file": ("bad", b"x")})
                    self.assertEqual(r.status_code, 400)
                    self.assertTrue(all(spool.closed for spool in spools))
                    self.assertEqual(list(root.iterdir()), [])

    def test_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".upload-old").mkdir()
            (root / ".upload-old" / "x").write_text("leftover")
            (root / "keep").mkdir()
            sweep(root)
            self.assertEqual(list(root.iterdir()), [root / "keep"])
