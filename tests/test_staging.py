"""Staging state transitions, bounded bodies and lifecycle races."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from calibre_mcp.staging import ID_RE, Stager, StagingError, safe_filename

UNKNOWN = "unknown or expired file_id; call prepare_upload again"
BUSY = "file_id is busy (an upload or import is in progress); retry shortly"


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.epoch = 1704067200.0

    def clock(self):
        return self.now

    def wall(self):
        return self.epoch


def request_for(chunks):
    chunks = iter(chunks)

    async def body():
        try:
            chunk = next(chunks)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        if isinstance(chunk, BaseException):
            raise chunk
        return {"type": "http.request", "body": chunk, "more_body": True}

    return Request({"type": "http", "method": "PUT", "path": "/", "headers": []}, body)


class StagingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "uploads"
        self.time = FakeClock()
        self.stager = Stager(self.root, ttl=10, max_bytes=8, max_entries=4,
                             clock=self.time.clock, wall=self.time.wall)
        self.override = None

        async def endpoint(request):
            try:
                staged = await self.stager.receive(request.path_params["file_id"], request, self.override)
                return JSONResponse({"size": staged.size, "sha256": staged.sha256})
            except StagingError as exc:
                return JSONResponse({"error": str(exc)}, status_code=exc.status)

        self.client = self.enterContext(TestClient(Starlette(routes=[
            Route("/{file_id}", endpoint, methods=["PUT"])])))

    def put(self, staged, content=b"hello", **kwargs):
        return self.client.put(f"/{staged.file_id}", content=content, **kwargs)

    def assert_error(self, call, status, message):
        with self.assertRaises(StagingError) as caught:
            call()
        self.assertEqual((caught.exception.status, str(caught.exception)), (status, message))

    def test_safe_filename(self):
        for name in ("../../book.epub", r"C:\books\book.epub", " .book.epub. ", "bo\x00ok.epub"):
            self.assertEqual(safe_filename(name), "book.epub")
        self.assertEqual(safe_filename("鸡蛋.epub"), "鸡蛋.epub")
        self.assertEqual(safe_filename("a" * 200 + ".epub"), "a" * 150 + ".epub")
        for name in ("..", "book", ".epub", "a/", "a\\", "a.*?"):
            with self.subTest(name=name):
                self.assert_error(lambda: safe_filename(name), 400,
                                  "filename must have a non-empty stem and extension")

    def test_prepare(self):
        self.assert_error(lambda: self.stager.prepare("bad"), 400,
                          "filename must have a non-empty stem and extension")
        entries = [self.stager.prepare("../book.epub") for _ in range(4)]
        self.assertEqual(len(self.stager), 4)
        self.assertEqual(len({s.file_id for s in entries}), 4)
        for staged in entries:
            self.assertIsNotNone(ID_RE.fullmatch(staged.file_id))
            self.assertEqual(staged.filename, "book.epub")
            self.assertEqual(staged.dir, self.root / f".staged-{staged.file_id}")
            self.assertTrue(staged.dir.is_dir())
            self.assertEqual(staged.path, staged.dir / "book.epub")
            self.assertEqual((staged.state, staged.expires, staged.size, staged.sha256, staged.has_bytes),
                             ("pending", 110, 0, None, False))
        self.assert_error(lambda: self.stager.prepare("book.epub"), 429,
                          "too many pending uploads (4); import or wait for them to expire")
        self.time.now = 110
        self.stager.prepare("new.epub")
        self.assertEqual(len(self.stager), 1)
        self.assertTrue(all(not s.dir.exists() for s in entries))

    def test_receive_and_replace(self):
        staged = self.stager.prepare("book.epub")
        self.time.now += 2
        response = self.put(staged)
        self.assertEqual((response.status_code, response.json()),
                         (200, {"size": 5, "sha256": hashlib.sha256(b"hello").hexdigest()}))
        self.assertEqual(staged.path.read_bytes(), b"hello")
        self.assertEqual((staged.state, staged.has_bytes, staged.expires), ("ready", True, 112))
        self.time.now += 2
        self.assertEqual(self.put(staged, b"new").status_code, 200)
        self.assertEqual((staged.path.read_bytes(), staged.size, staged.sha256, staged.expires),
                         (b"new", 3, hashlib.sha256(b"new").hexdigest(), 114))
        self.assertEqual(list(staged.dir.iterdir()), [staged.path])

    def test_receive_errors_preserve_completed_bytes(self):
        for initial in (False, True):
            staged = self.stager.prepare("book.epub")
            if initial:
                self.put(staged, b"old")
            for content, headers, status, message in (
                (b"", {}, 400, "empty upload"),
                (b"x", {"Content-Length": "bad"}, 400, "invalid Content-Length"),
                (b"x", {"Content-Length": "-1"}, 400, "invalid Content-Length"),
                (b"x", {"Content-Length": "1.5"}, 400, "invalid Content-Length"),
                (b"x", {"Content-Length": "9"}, 413, "upload larger than 7.62939e-06 MB"),
                (iter([b"12345", b"6789"]), {}, 413, "upload larger than 7.62939e-06 MB"),
            ):
                with self.subTest(initial=initial, headers=headers, status=status):
                    response = self.put(staged, content, headers=headers)
                    self.assertEqual((response.status_code, response.json()), (status, {"error": message}))
                    self.assertEqual(staged.state, "ready" if initial else "pending")
                    self.assertEqual(staged.has_bytes, initial)
                    self.assertFalse((staged.dir / ".part").exists())
                    if initial:
                        self.assertEqual((staged.path.read_bytes(), staged.size, staged.sha256),
                                         (b"old", 3, hashlib.sha256(b"old").hexdigest()))
                    else:
                        self.assertFalse(staged.path.exists())
            self.stager.consume(staged.file_id)

    def test_length_checked_before_read_and_override(self):
        staged = self.stager.prepare("book.epub")

        async def check():
            request = request_for([AssertionError("must not read")])
            request.scope["headers"] = [(b"content-length", b"9")]
            with self.assertRaises(StagingError) as caught:
                await self.stager.receive(staged.file_id, request)
            self.assertEqual(caught.exception.status, 413)
        asyncio.run(check())
        self.override = 2
        self.assertEqual(self.put(staged, b"abc").status_code, 413)
        self.assertEqual(self.put(staged, iter([b"a", b"bc"])).status_code, 413)
        self.assertEqual(self.put(staged, b"ab").status_code, 200)
        self.override = 0
        self.assertEqual(self.put(staged, b"a").status_code, 413)
        self.override = 12
        self.assertEqual(self.put(staged, b"123456789").status_code, 200)

    def test_unknown_and_expired_ids(self):
        for file_id in ("bad", "a" * 32, "a" * 33, "a" * 32 + "\n", ".."):
            self.assert_error(lambda: self.stager.claim(file_id), 404, UNKNOWN)
            # Direct receive also covers IDs that cannot be represented as one URL path segment.
            async def check():
                with self.assertRaises(StagingError) as caught:
                    await self.stager.receive(file_id, request_for([AssertionError("must not read")]))
                self.assertEqual((caught.exception.status, str(caught.exception)), (404, UNKNOWN))
            asyncio.run(check())
        self.assertEqual(self.client.put("/bad", content=b"a").status_code, 404)
        staged = self.stager.prepare("book.epub")
        self.time.now = staged.expires
        self.assertEqual(self.put(staged).status_code, 404)
        self.assertFalse(staged.dir.exists())
        staged = self.stager.prepare("book.epub")
        self.put(staged)
        self.time.now = staged.expires
        self.assert_error(lambda: self.stager.claim(staged.file_id), 404, UNKNOWN)
        self.assertFalse(staged.dir.exists())

    def test_claim_release_consume(self):
        staged = self.stager.prepare("book.epub")
        self.assertIsNone(self.stager.release(staged.file_id))
        self.assert_error(lambda: self.stager.claim(staged.file_id), 409,
                          "no bytes uploaded yet for this file_id; PUT the file to upload_url first")
        self.put(staged)
        self.assertIsNone(self.stager.release(staged.file_id))
        self.assertIs(self.stager.claim(staged.file_id), staged)
        self.assertEqual(staged.state, "importing")
        self.assert_error(lambda: self.stager.claim(staged.file_id), 409, BUSY)
        response = self.put(staged)
        self.assertEqual((response.status_code, response.json()), (409, {"error": BUSY}))
        self.time.now = 200
        self.assertEqual(self.stager.reap(), 0)
        self.assertTrue(staged.dir.exists())
        self.assertIs(self.stager.release(staged.file_id), staged)
        self.assertEqual((staged.state, staged.expires), ("ready", 210))
        self.assertIs(self.stager.claim(staged.file_id), staged)
        self.stager.consume(staged.file_id)
        self.assertEqual(len(self.stager), 0)
        self.assertFalse(staged.dir.exists())
        self.assertIsNone(self.stager.release(staged.file_id))
        self.stager.consume(staged.file_id)
        self.assert_error(lambda: self.stager.claim(staged.file_id), 404, UNKNOWN)

    def test_reap_and_expiry_helpers(self):
        pending = self.stager.prepare("pending.epub")
        ready = self.stager.prepare("ready.epub")
        self.put(ready)
        self.assertEqual(self.stager.expires_at(ready), "2024-01-01T00:00:10Z")
        self.assertEqual(self.stager.expires_in(ready), 10)
        self.time.now += 3
        self.time.epoch += 5
        self.assertEqual(self.stager.expires_at(ready), "2024-01-01T00:00:12Z")
        self.assertEqual(self.stager.expires_in(ready), 7)
        self.time.now = 110
        self.assertEqual(self.stager.reap(), 2)
        self.assertEqual(self.stager.expires_in(ready), 0)
        self.time.now += 1
        self.assertEqual(self.stager.expires_in(ready), 0)
        self.assertFalse(pending.dir.exists())
        self.assertFalse(ready.dir.exists())
        self.assertEqual(self.stager.reap(), 0)

    def test_stream_failure_and_atomic_replace_failure(self):
        for initial in (False, True):
            staged = self.stager.prepare("book.epub")
            if initial:
                self.put(staged, b"old")

            async def check():
                with self.assertRaisesRegex(RuntimeError, "broken stream"):
                    await self.stager.receive(staged.file_id, request_for([b"a", RuntimeError("broken stream")]))
            asyncio.run(check())
            with patch("calibre_mcp.staging.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    asyncio.run(self.stager.receive(staged.file_id, request_for([b"new"])))
            self.assertEqual(staged.state, "ready" if initial else "pending")
            self.assertFalse((staged.dir / ".part").exists())
            if initial:
                self.assertEqual(staged.path.read_bytes(), b"old")
            self.stager.consume(staged.file_id)

    def test_receive_races_and_cancellation(self):
        async def scenario(outcome, initial):
            staged = self.stager.prepare("book.epub")
            if initial:
                await self.stager.receive(staged.file_id, request_for([b"old"]))
            started = asyncio.Event()
            proceed = asyncio.Event()
            calls = 0

            async def body():
                nonlocal calls
                calls += 1
                if calls == 1:
                    return {"type": "http.request", "body": b"new", "more_body": True}
                started.set()
                await proceed.wait()
                if outcome == "failure":
                    raise RuntimeError("broken stream")
                return {"type": "http.request", "body": b"", "more_body": False}

            request = Request({"type": "http", "method": "PUT", "path": "/", "headers": []}, body)
            task = asyncio.create_task(self.stager.receive(staged.file_id, request))
            await started.wait()
            self.assertEqual(staged.state, "uploading")
            self.assert_error(lambda: self.stager.claim(staged.file_id), 409, BUSY)
            with self.assertRaises(StagingError) as caught:
                await self.stager.receive(staged.file_id, request_for([b"other"]))
            self.assertEqual((caught.exception.status, str(caught.exception)), (409, BUSY))
            self.assertIsNone(self.stager.release(staged.file_id))
            if outcome != "cancel":
                self.time.now = staged.expires + 1
            self.assertEqual(self.stager.reap(), 0)
            self.assertTrue(staged.dir.exists())
            if outcome == "consume":
                self.stager.consume(staged.file_id)
            if outcome == "cancel":
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(staged.state, "ready" if initial else "pending")
                self.assertFalse((staged.dir / ".part").exists())
                if initial:
                    self.assertEqual(staged.path.read_bytes(), b"old")
                self.stager.consume(staged.file_id)
            else:
                proceed.set()
                if outcome == "failure":
                    with self.assertRaisesRegex(RuntimeError, "broken stream"):
                        await task
                elif outcome == "consume":
                    with self.assertRaises(StagingError) as caught:
                        await task
                    self.assertEqual(caught.exception.status, 404)
                else:
                    self.assertIs(await task, staged)
                    self.assertEqual((staged.state, staged.expires), ("ready", self.time.now + 10))
                    self.assertEqual(staged.path.read_bytes(), b"new")
                    self.stager.consume(staged.file_id)
                self.assertEqual(len(self.stager), 0)
                self.assertFalse(staged.dir.exists())

        for initial in (False, True):
            for outcome in ("failure", "success", "cancel", "consume"):
                with self.subTest(initial=initial, outcome=outcome):
                    asyncio.run(scenario(outcome, initial))

    def test_sweep(self):
        self.stager.sweep()  # Missing root is harmless.
        self.root.mkdir()
        for name in (".staged-old", ".upload-old", "keep", ".staged", ".upload"):
            directory = self.root / name
            directory.mkdir()
            (directory / "bytes").write_bytes(b"leftover")
        for name in (".staged-file", ".upload-file"):
            (self.root / name).write_bytes(b"keep")
        self.stager.sweep()
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {"keep", ".staged", ".upload", ".staged-file", ".upload-file"})
