"""Configuration and file import tests, including a fake and optional real calibredb."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calibre_mcp import config as config_module
from calibre_mcp.config import Config, LibraryConfig, load_config
from calibre_mcp.importer import DUPLICATES, LOCKED, AddError, AddResult, BookMetadata, Importer, parse_output


class ConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = load_config(env={})
        self.assertEqual((cfg.calibredb, cfg.add_timeout, cfg.max_upload_mb), ("calibredb", 300, 1024))
        self.assertEqual((cfg.upload_ttl, cfg.max_staged_uploads), (3600, 16))

    def test_toml_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('calibredb = "custom-db"\nadd_timeout = 12\nmax_upload_mb = 64\n')
            cfg = load_config(config, env={})
            self.assertEqual((cfg.calibredb, cfg.add_timeout, cfg.max_upload_mb), ("custom-db", 12, 64))
            cfg = load_config(config, env={"CALIBRE_MCP_CALIBREDB": "env-db", "CALIBRE_MCP_MAX_UPLOAD_MB": "32"})
            self.assertEqual((cfg.calibredb, cfg.add_timeout, cfg.max_upload_mb), ("env-db", 12, 32))

    def test_staging_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "metadata.db").touch()
            path = root / "config.toml"
            path.write_text('upload_ttl = 120\nmax_staged_uploads = 3\n')
            cfg = load_config(path, env={})
            self.assertEqual((cfg.upload_ttl, cfg.max_staged_uploads), (120, 3))
            cfg = load_config(path, env={"CALIBRE_MCP_UPLOAD_TTL": "60", "CALIBRE_MCP_MAX_STAGED_UPLOADS": "1"})
            self.assertEqual((cfg.upload_ttl, cfg.max_staged_uploads), (60, 1))
            cfg.libraries = [LibraryConfig("main", root)]
            cfg.validate()
            for key, values in (("upload_ttl", (59, 0, -1, 60.5, True)),
                                ("max_staged_uploads", (0, -1, 1.5, True))):
                old = getattr(cfg, key)
                for value in values:
                    setattr(cfg, key, value)
                    with self.subTest(key=key, value=value), self.assertRaisesRegex(SystemExit, key):
                        cfg.validate()
                setattr(cfg, key, old)

    def test_path_maps_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('[[path_maps]]\nfrom = "/host"\nto = "/inbox"\n')
            cfg = load_config(config, env={"CALIBRE_MCP_PATH_MAPS": "invalid old setting"})
            self.assertFalse(hasattr(cfg, "path_maps"))
            self.assertFalse(hasattr(config_module, "PathMap"))
            self.assertFalse(hasattr(config_module, "parse_path_maps"))

    def test_validate_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "metadata.db").touch()
            cfg = Config(libraries=[LibraryConfig("main", root)], add_timeout=1, max_upload_mb=1)
            cfg.validate()
            for field in ("add_timeout", "max_upload_mb"):
                for value in (0, -1):
                    with self.subTest(field=field, value=value):
                        setattr(cfg, field, value)
                        with self.assertRaisesRegex(SystemExit, field + " must be at least 1"):
                            cfg.validate()
                        setattr(cfg, field, 1)


class BookMetadataTest(unittest.TestCase):
    def test_every_field(self) -> None:
        meta = BookMetadata(title=" Title ", authors=[" First ", "Second"], tags=[" one ", "", "two"],
                            series=" Series ", series_index=2, languages=[" eng ", " ", "fra"],
                            identifiers={"isbn": " 123 ", " DOI ": " x "})
        self.assertEqual(meta.args(), ["--title", "Title", "--authors", "First & Second", "--tags", "one,two",
                                       "--series", "Series", "--series-index", "2.0", "--languages", "eng,fra",
                                       "--identifier", "doi:x", "--identifier", "isbn:123"])
        self.assertEqual(BookMetadata().args(), [])
        self.assertEqual(BookMetadata(tags=[" "], languages=[""], series=" ").args(), [])
        self.assertEqual(BookMetadata(series="S", series_index=0).args(), ["--series", "S", "--series-index", "0.0"])

    def test_invalid_fields(self) -> None:
        for meta in (BookMetadata(title=" "), BookMetadata(authors=[""]), BookMetadata(authors=["A & B"]),
                     BookMetadata(tags=["a,b"]), BookMetadata(languages=["eng,fra"]),
                     BookMetadata(series_index=1), BookMetadata(series=" ", series_index=1),
                     BookMetadata(identifiers={"bad:key": "x"}), BookMetadata(identifiers={"": "x"}),
                     BookMetadata(identifiers={"isbn": " "}), BookMetadata(identifiers={"isbn": "a,b"})):
            with self.subTest(meta=meta), self.assertRaises(AddError) as caught:
                meta.args()
            self.assertEqual(caught.exception.status, 400)


class ParseOutputTest(unittest.TestCase):
    def test_results(self) -> None:
        self.assertEqual(parse_output("noise\nAdded book ids: 4\n"), AddResult("added", [4], []))
        self.assertEqual(parse_output("Merged book ids: 1, 2\n"), AddResult("merged", [1, 2], []))
        self.assertEqual(parse_output("Merged book ids: 1, 2\nAdded book ids: 4\n"),
                         AddResult("added", [4, 1, 2], []))
        self.assertEqual(parse_output("The following books were not added as they already exist in the database\n"
                                      "(see --duplicates option or --automerge option):\n"
                                      "  First title\n    /path/one.txt\n  Second title\n    /path/two.txt\n"),
                         AddResult("duplicate", [], ["First title", "Second title"]))

    def test_calibre_8_5_duplicate_report_on_stderr(self) -> None:
        stdout = "    /tmp/calibre-packaging-probe/inbox/book.txt\n"
        stderr = ("The following books were not added as they already exist in the database "
                  "(see --duplicates option or --automerge option):\n"
                  "  Importer test book\n")
        self.assertEqual(parse_output(stdout, stderr), AddResult("duplicate", [], ["Importer test book"]))
        self.assertEqual(parse_output(stdout, stderr + "  Second title\nTraceback: reader noise\n  ignored\n"),
                         AddResult("duplicate", [], ["Importer test book", "Second title"]))

    def test_stderr_requires_explicit_duplicate_report(self) -> None:
        for stderr in ("Traceback: bad format\nMerged book ids: 99\nAdded book ids: 10\n",
                       "Traceback: books already exist in the database\n  Not a duplicate title\n",
                       "The following books were not added as they already exist in the database "
                       "(see --duplicates option or --automerge option):\nTraceback: reader failure\n"):
            with self.subTest(stderr=stderr), self.assertRaises(AddError) as caught:
                parse_output("    /tmp/inbox/book.txt\n", stderr)
            self.assertEqual(caught.exception.status, 502)

    def test_failure_diagnostic_tail(self) -> None:
        for stdout in ("", "unrecognized stdout", "Added book ids: nope"):
            with self.subTest(stdout=stdout), self.assertRaises(AddError) as caught:
                parse_output(stdout, "omit\na\nb\nc\nd\nreader failed: useful diagnostic\n")
            self.assertEqual(caught.exception.status, 502)
            self.assertEqual(str(caught.exception),
                             "calibredb added nothing: a | b | c | d | reader failed: useful diagnostic")

    def test_failure_diagnostic_falls_back_to_stdout(self) -> None:
        for stderr in ("", " \n"):
            with self.subTest(stderr=stderr), self.assertRaises(AddError) as caught:
                parse_output("omit\na\nb\nc\nd\nstdout diagnostic\n", stderr)
            self.assertEqual(caught.exception.status, 502)
            self.assertEqual(str(caught.exception), "calibredb added nothing: a | b | c | d | stdout diagnostic")

    def test_garbage(self) -> None:
        for stdout in ("", "file not found", "Added book ids: nope", "unexpected output"):
            with self.subTest(stdout=stdout), self.assertRaisesRegex(AddError, "calibredb added nothing") as caught:
                parse_output(stdout)
            self.assertEqual(caught.exception.status, 502)


def fake_calibredb(path: Path, control: Path, record: Path) -> Path:
    path.write_text(f"#!{sys.executable}\n"
                    "import json, os, sys, time\n"
                    "from pathlib import Path\n"
                    f"control = json.loads(Path({str(control)!r}).read_text())\n"
                    f"Path({str(record)!r}).write_text(json.dumps({{'argv': sys.argv[1:], "
                    "'home': os.environ['HOME'], 'qt': os.environ['QT_QPA_PLATFORM'], "
                    "'home_exists': Path(os.environ['HOME']).is_dir()}))\n"
                    "time.sleep(control.get('sleep', 0))\n"
                    "print(control.get('stdout', 'Added book ids: 4'))\n"
                    "sys.stderr.write(control.get('stderr', ''))\n"
                    "sys.exit(control.get('exit', 0))\n")
    path.chmod(0o755)
    return path


class ImporterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.book = self.inbox / "--book.txt"
        self.book.write_text("text")
        self.control = self.root / "control.json"
        self.control.write_text("{}")
        self.record = self.root / "argv.json"
        self.exe = fake_calibredb(self.root / "calibredb", self.control, self.record)
        self.importer = Importer(str(self.exe))
        self.library = self.root / "library"

    async def test_modes_and_environment(self) -> None:
        self.assertTrue(self.importer.available)
        self.assertEqual(DUPLICATES, ("refuse", "new_record", "merge"))
        for mode, flags in (("refuse", []), ("new_record", ["--duplicates"]),
                            ("merge", ["--automerge", "ignore"])):
            with self.subTest(mode=mode):
                result = await self.importer.add(self.library, self.book, BookMetadata(title="T"), mode)
                self.assertEqual(result, AddResult("added", [4], []))
                record = json.loads(self.record.read_text())
                self.assertEqual(record["argv"], ["add", "--with-library", str(self.library), *flags,
                                                  "--title", "T", "--", str(self.book)])
                self.assertEqual(record["qt"], "offscreen")
                self.assertTrue(record["home_exists"])
                self.assertFalse(Path(record["home"]).exists())
                self.assertIn("calibre-mcp-add-", record["home"])

    async def test_stderr_noise_does_not_change_result(self) -> None:
        self.control.write_text(json.dumps({"stderr": "Traceback: bad format\nMerged book ids: 99"}))
        result = await self.importer.add(self.library, self.book, BookMetadata())
        self.assertEqual(result, AddResult("added", [4], []))

    async def test_calibre_8_5_duplicate_report_on_stderr(self) -> None:
        self.control.write_text(json.dumps({
            "stdout": f"    {self.book}\n",
            "stderr": ("The following books were not added as they already exist in the database "
                       "(see --duplicates option or --automerge option):\n"
                       "  Importer test book\n"),
        }))
        result = await self.importer.add(self.library, self.book, BookMetadata())
        self.assertEqual(result, AddResult("duplicate", [], ["Importer test book"]))
        self.control.write_text(json.dumps({"stdout": f"    {self.book}\n",
                                           "stderr": "Traceback: reader failure\nMerged book ids: 99\n"}))
        with self.assertRaisesRegex(AddError, "calibredb added nothing") as caught:
            await self.importer.add(self.library, self.book, BookMetadata())
        self.assertEqual(caught.exception.status, 502)

    async def test_errors(self) -> None:
        cases = [({"stderr": LOCKED, "exit": 1}, "library is open in another Calibre program", 503),
                 ({"stdout": LOCKED}, "library is open in another Calibre program", 503),
                 ({"stderr": "bad input", "exit": 2}, "calibredb failed: bad input", 502),
                 ({"stdout": "failed stdout", "exit": 2}, "calibredb failed: failed stdout", 502),
                 ({"stdout": ""}, "calibredb added nothing", 502)]
        for control, message, status in cases:
            with self.subTest(control=control), self.assertRaisesRegex(AddError, message) as caught:
                self.control.write_text(json.dumps(control))
                await self.importer.add(self.library, self.book, BookMetadata())
            self.assertEqual(caught.exception.status, status)

    async def test_failure_tail(self) -> None:
        self.control.write_text(json.dumps({"exit": 1, "stderr": "omit\na\nb\nc\nd\ne"}))
        with self.assertRaisesRegex(AddError, r"^calibredb failed: a \| b \| c \| d \| e$"):
            await self.importer.add(self.library, self.book, BookMetadata())

    async def test_timeout(self) -> None:
        self.control.write_text(json.dumps({"sleep": 3}))
        importer = Importer(str(self.exe), timeout=1)
        with self.assertRaisesRegex(AddError, "calibredb timed out after 1s") as caught:
            await importer.add(self.library, self.book, BookMetadata())
        self.assertEqual(caught.exception.status, 502)

    async def test_timeout_diagnostic_tail(self) -> None:
        cases = [
            ("ignored stdout", "omit\na\nb\nc\nd\nreader stalled", "a | b | c | d | reader stalled"),
            (b"ignored stdout", b"omit\na\nb\nc\nd\nreader stalled: \xff", "a | b | c | d | reader stalled: \ufffd"),
            ("omit\na\nb\nc\nd\nstdout diagnostic", "", "a | b | c | d | stdout diagnostic"),
            (b"omit\na\nb\nc\nd\nstdout diagnostic: \xff", b"", "a | b | c | d | stdout diagnostic: \ufffd"),
            ("stdout diagnostic", " \n", "stdout diagnostic"),
            (None, None, ""),
            (b"", b"", ""),
        ]
        for stdout, stderr, tail in cases:
            timeout = subprocess.TimeoutExpired("calibredb", self.importer.timeout, output=stdout, stderr=stderr)
            with self.subTest(stdout=stdout, stderr=stderr), patch(
                "calibre_mcp.importer.subprocess.run", side_effect=timeout
            ), self.assertRaises(AddError) as caught:
                await self.importer.add(self.library, self.book, BookMetadata())
            self.assertEqual(caught.exception.status, 502)
            self.assertEqual(str(caught.exception), "calibredb timed out after 300s" + (": " + tail if tail else ""))

    async def test_unavailable(self) -> None:
        importer = Importer(str(self.root / "missing-exe"))
        self.assertFalse(importer.available)
        with self.assertRaisesRegex(AddError, "not installed") as caught:
            await importer.add(self.library, self.book, BookMetadata())
        self.assertEqual(caught.exception.status, 503)
        self.assertFalse(self.record.exists())

    async def test_validation_before_process_and_lock(self) -> None:
        extensionless = self.inbox / "noextension"
        extensionless.touch()
        lock = asyncio.Lock()
        await lock.acquire()
        self.importer._locks[str(self.library)] = lock
        try:
            cases = [(self.inbox / "missing.txt", BookMetadata(), "refuse"),
                     (extensionless, BookMetadata(), "refuse"),
                     (self.inbox, BookMetadata(), "refuse"),
                     (self.book, BookMetadata(title=" "), "refuse"),
                     (self.book, BookMetadata(), "unknown")]
            for src, metadata, mode in cases:
                with self.subTest(src=src, metadata=metadata, mode=mode), self.assertRaises(AddError) as caught:
                    await asyncio.wait_for(self.importer.add(self.library, src, metadata, mode), timeout=1)
                self.assertEqual(caught.exception.status, 400)
        finally:
            lock.release()
        self.assertFalse(self.record.exists())

    async def test_serializes_same_library(self) -> None:
        active = set()
        calls = []

        async def run(func, cmd):
            library = cmd[3]
            self.assertNotIn(library, active)
            active.add(library)
            calls.append(library)
            await asyncio.sleep(0)
            active.remove(library)
            return "Added book ids: 4", ""

        with patch("calibre_mcp.importer.asyncio.to_thread", side_effect=run):
            await asyncio.gather(*(self.importer.add(self.library, self.book, BookMetadata()) for _ in range(3)))
        self.assertEqual(calls, [str(self.library)] * 3)


@unittest.skipUnless(shutil.which("calibredb"), "calibredb is not installed")
class RealCalibredbTest(unittest.IsolatedAsyncioTestCase):
    async def test_add_duplicate_and_new_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "library"
            library.mkdir()
            home = root / "home"
            home.mkdir()
            proc = subprocess.run(["calibredb", "--with-library", str(library), "list"],
                                  capture_output=True, text=True, timeout=60,
                                  env=dict(os.environ, HOME=str(home), QT_QPA_PLATFORM="offscreen"))
            if LOCKED in proc.stdout or LOCKED in proc.stderr:
                self.skipTest("another Calibre program is running")
            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
            inbox = root / "inbox"
            inbox.mkdir()
            book = inbox / "book.txt"
            book.write_text("A book for the importer integration test.\n")
            importer = Importer("calibredb")
            metadata = BookMetadata(title="Importer test book", authors=["Test Author"])
            try:
                first = await importer.add(library, book, metadata)
                duplicate = await importer.add(library, book, metadata)
                another = await importer.add(library, book, metadata, "new_record")
            except AddError as exc:
                if "library is open in another Calibre program" in str(exc):
                    self.skipTest(str(exc))
                raise
            self.assertEqual(first.status, "added")
            self.assertEqual(len(first.book_ids), 1)
            self.assertEqual(duplicate, AddResult("duplicate", [], ["Importer test book"]))
            self.assertEqual(another.status, "added")
            self.assertEqual(len(another.book_ids), 1)
            self.assertNotEqual(first.book_ids, another.book_ids)


if __name__ == "__main__":
    unittest.main()
