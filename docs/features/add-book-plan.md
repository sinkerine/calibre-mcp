# Feature plan: add a book to a Calibre library

Status: **approved by the user (2026-11-07)**, including implementation, review, E2E and the automatic final merge
to the default branch.
Branch `feat/add-book`, base `038d746`.

## Goal and acceptance criteria

An MCP client can add an ebook file that already sits on the server's filesystem to a configured
Calibre library. The client gives the path as **it** sees it (e.g. a host path). The server translates
that path into its own mount path (e.g. inside Docker) through configured path maps, and imports the
file with Calibre's own `calibredb add`. The server keeps no other write path: reads stay read-only
SQLite queries, as they are today.

Decisions made with the user:

| Topic | Decision |
| --- | --- |
| Input | MCP tool `add_book(path=…)` with a **server-side path**. No uploads, URLs or base64. |
| Path translation | Prefix map list: `[[path_maps]] from = "<client path>"`, `to = "<server path>"`. |
| Path scope | Only files that resolve (after `..` and symlink resolution) inside a configured `to` directory. With no maps configured, every add is refused. |
| Write mechanism | `calibredb add --with-library <library root>` subprocess. No direct SQLite writes. |
| Duplicates | Default `refuse` returns the existing matches. The caller can opt in to `new_record` (`calibredb --duplicates`) or `merge` (`--automerge ignore`). |
| Enablement | Always on (no feature flag). It works only when `calibredb` is installed **and** at least one path map is configured. |
| Extras | Optional metadata overrides: title, authors, tags, series, series_index, languages, identifiers. |

Acceptance criteria (each one is checked by a unit test or an E2E step):

1. `tools/list` includes `add_book` next to the existing 4 tools (5 tools in total).
2. With `path_maps = [{from="/host/inbox", to="/import"}]`, `add_book(path="/host/inbox/x.epub")` imports
   `/import/x.epub` and returns `status="added"` plus the new book's full `get_book` metadata and links. The book
   then appears in `search_books` and its file downloads from `/<lib>/books/<id>/epub`.
3. A path already under a `to` dir (`/import/x.epub`) is accepted unchanged.
4. Every path outside every import dir is refused with a tool error, and the library is unchanged. This covers
   paths outside the maps, `..` escapes, symlinks pointing outside, and relative paths.
5. A missing file, a directory, or a file without an extension is refused with a tool error before `calibredb` runs.
6. Adding the same title and authors again with the default `duplicates="refuse"` returns `status="duplicate"`
   (not a tool error) with `existing` candidate books, and adds no record. `duplicates="new_record"` adds a second
   record. `duplicates="merge"` adds the new format to the matching record(s) and returns `status="merged"`, or
   `status="duplicate"` when that format is already there.
7. Metadata overrides reach `calibredb` as the correct options, and invalid values are rejected with a tool error
   before `calibredb` runs. Invalid values are: an empty title, an author containing `&`, a tag or language
   containing `,`, a bad identifier key, an identifier value containing `,`, and `series_index` without `series`.
8. `calibredb` missing → tool error "calibredb is not installed…". No path maps → tool error "adding books is not
   configured…". The "Another calibre program … is running" lock → a clear tool error. Timeout or non-zero exit →
   a tool error with the tail of stderr. Output that can't be parsed → a tool error.
9. `list_libraries` reports `add_books` (bool) and `import_dirs` (the client-side `from` paths). `GET /` reports
   `add_books`. `calibre-mcp check` prints add-books status.
10. Config: TOML `[[path_maps]]`, `calibredb`, `add_timeout`; env `CALIBRE_MCP_PATH_MAPS`, `CALIBRE_MCP_CALIBREDB`;
    flags `--path-map FROM=TO` (repeatable, replaces configured maps) and `--calibredb`. `validate()` rejects a
    relative `from`/`to` and a `to` that is not an existing directory.
11. All existing tests still pass, and the README documents the feature, including the rw library mount.

Non-goals: HTTP upload or JSON endpoint, URL fetch, base64 content, adding a format to a chosen book id, deleting
the source file, cover override, auth.

## Design

### What `calibredb add` actually does (probed with calibre 9.17)

- On success, stdout has `Added book ids: 4`. With `--automerge`, stdout has `Merged book ids: 1, 2`, and merges
  can touch **several** existing records.
- When it refuses a duplicate, stdout has `The following books were not added as they already exist in the database
  (see --duplicates option or --automerge option):`, then for each book a line with the title (2-space indent) and
  a line with the path (4-space indent). **Exit code 0.**
- A missing file prints `<path> not found` with **exit code 0**. A file without an extension prints nothing, also
  with exit 0. So we pre-validate the file and treat "nothing recognised" as an error.
- Another Calibre program (GUI or calibre-server) running for the same user/namespace → `Another calibre program
  such as calibre-server or the main calibre program is running…`, exit 1. Containers have their own namespace, so
  the check does **not** see a desktop Calibre on the host. The README must warn against adding while the desktop
  app has the same library open.
- Format readers write tracebacks to stderr even on success (e.g. a bad PDF info dict). Parse **stdout** only.
- `calibredb --with-library <root> list` on an empty dir creates a fresh `metadata.db` (used by tests and E2E).
- Option order is flexible, and `--` before the file path works.

### Module boundaries

```
config.py    PathMap, Config.path_maps / calibredb / add_timeout, parse_path_maps(), validate()   [T1]
importer.py  NEW: path translation + scope check, metadata → calibredb args, calibredb runner,
             output parsing. No MCP/Starlette imports.                                         [T1]
server.py    Catalog.importer / can_add, add_book MCP tool, list_libraries + index fields,
             INSTRUCTIONS text                                                                 [T2]
__main__.py  --path-map, --calibredb flags; `check` prints add status                           [T2]
library.py   unchanged (reads stay read-only; new book read back through Library.book())
```

Data flow: MCP `add_book` → `Catalog.library(name)` → `Importer.add(lib.root, path, BookMetadata, duplicates)` →
`translate()` → per-library `asyncio.Lock` → `asyncio.to_thread(_run)` → `calibredb` subprocess → `parse_output()` →
`AddResult`. Then the server uses `Library.book(id)` (fresh read-only connection) and `Catalog.links()` to build the
response.

### `config.py` (T1)

```python
@dataclass
class PathMap:
    source: str    # TOML "from": absolute directory as MCP clients see it (e.g. host path)
    target: Path   # TOML "to": the same directory as this server sees it (e.g. container mount)

@dataclass
class Config:
    ...existing fields...
    path_maps: list[PathMap] = field(default_factory=list)
    calibredb: str = "calibredb"
    add_timeout: int = 300

def parse_path_maps(spec: str) -> list[PathMap]:
    """"FROM=TO,FROM=TO" → PathMaps (env / CLI form). Items are stripped; empty items skipped.
    An item without "=" raises SystemExit(f"bad path map {item!r} (FROM=TO)")."""
```

- `load_config`: TOML `path_maps = [{from, to}]` (i.e. `[[path_maps]]`), `calibredb`, `add_timeout`. Env
  `CALIBRE_MCP_PATH_MAPS` (replaces the TOML maps) and `CALIBRE_MCP_CALIBREDB`. Apply `expanduser()` to `target` only.
  `source` is a client-side string and is normalised with `posixpath.normpath`.
- `validate()` additions: for each map, `source` must be absolute (`PurePosixPath(source).is_absolute()`), and
  `target` must be absolute and `is_dir()`. Otherwise raise `SystemExit("path map {source!r} → {target}: …")`.
- Update the module docstring with the new keys and env vars.

### `importer.py` (T1, new)

```python
from typing import Literal

Duplicates = Literal["refuse", "new_record", "merge"]
LOCKED = "Another calibre program"

class AddError(RuntimeError):
    """A user-facing reason the book was not added (becomes a ToolError)."""

@dataclass(frozen=True)
class BookMetadata:
    title: str | None = None
    authors: list[str] | None = None
    tags: list[str] | None = None
    series: str | None = None
    series_index: float | None = None
    languages: list[str] | None = None
    identifiers: dict[str, str] | None = None

    def args(self) -> list[str]:
        """calibredb add options for the fields that are set; raises AddError on invalid values."""

@dataclass
class AddResult:
    status: Literal["added", "merged", "duplicate"]
    book_ids: list[int]           # added or merged ids; [] for "duplicate"
    duplicate_titles: list[str]   # titles calibredb reported as already in the library

def translate(path: str, maps: list[PathMap]) -> Path:
    """Map a client path to the real path of an existing file inside an import dir; raises AddError."""

def parse_output(stdout: str) -> AddResult:
    """Interpret calibredb add stdout; raises AddError when nothing recognisable was reported."""

class Importer:
    def __init__(self, exe: str, maps: list[PathMap], *, timeout: int = 300) -> None: ...
    @property
    def available(self) -> bool: ...        # shutil.which(exe) found
    @property
    def configured(self) -> bool: ...       # at least one path map
    @property
    def import_dirs(self) -> list[str]: ... # map sources, for clients
    def command(self, library_root: Path, src: Path, metadata: BookMetadata,
                duplicates: Duplicates = "refuse") -> list[str]: ...
    async def add(self, library_root: Path, path: str, metadata: BookMetadata,
                  duplicates: Duplicates = "refuse") -> AddResult: ...
```

`translate(path, maps)`:
1. `path` must be a non-empty absolute POSIX path. Otherwise raise `AddError("path must be absolute: …")`.
   Normalise it with `posixpath.normpath`, which collapses `..`.
2. Find the map whose `source` is a **component-wise** prefix (`PurePosixPath.is_relative_to`). The longest `source`
   wins. The candidate is `target / relative`. If no map matches but the path is already under some `target`, the
   path itself is the candidate. Otherwise raise `AddError("{path} is not inside an import directory ({sources})")`.
3. `real = Path(candidate).resolve(strict=True)`. A `FileNotFoundError` becomes `AddError("no such file: {path}")`.
4. `real` must be `is_relative_to(t.resolve())` for some map target, which blocks symlink escapes. If it isn't,
   raise the same "not inside an import directory" error.
5. Require `real.is_file()` (a directory is not a file → `AddError`) and a non-empty `real.suffix`
   (`AddError("… has no file extension; Calibre picks the format from it")`).
6. Return `real`. Error messages quote the **client** path, never the internal mount path.

`BookMetadata.args()`. Strip every string; an empty string after stripping counts as not set, except `title`,
where an explicit empty title raises an error.
- `title` → `["--title", t]`
- `authors` → `["--authors", " & ".join(a)]`. An element containing `&` or empty → `AddError`.
- `tags` → `["--tags", ",".join(t)]`. An element containing `,` → `AddError`.
- `series` → `["--series", s]`. `series_index` → `["--series-index", repr(float)]`, and it needs `series`.
- `languages` → `["--languages", ",".join(l)]`. An element containing `,` → `AddError`.
- `identifiers` → one `["--identifier", f"{k.lower()}:{v}"]` per item, in sorted key order. The key must match
  `^[A-Za-z0-9_-]+$`, and the value must be non-empty with no `,`.

`Importer.command()` returns:
`[exe, "add", "--with-library", str(library_root), *dup_flags, *metadata.args(), "--", str(src)]`, where
`dup_flags` is `[]` for refuse, `["--duplicates"]` for new_record, and `["--automerge", "ignore"]` for merge.

`Importer.add()` steps:
1. `not available` → `AddError("calibredb is not installed; adding books needs Calibre")`.
2. `not configured` → `AddError("adding books is not configured: set path_maps (CALIBRE_MCP_PATH_MAPS)")`.
3. `src = translate(path, maps)`, then `cmd = self.command(...)`. Both run **before** taking the lock and before any
   subprocess, so validation errors never start `calibredb`.
4. Take the per-library-root `asyncio.Lock` (dict keyed by `str(library_root)`, the same pattern as `Converter`).
   Then run `await asyncio.to_thread(self._run, cmd)`.
5. `_run(cmd) -> str` runs `subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, env=…)`
   inside a `tempfile.TemporaryDirectory(prefix="calibre-mcp-add-")`, with
   `env = dict(os.environ, HOME=tmp, QT_QPA_PLATFORM="offscreen")`. Calibre writes prefs under `$HOME`, and
   containers may have no writable home.
   - `TimeoutExpired` → `AddError(f"calibredb timed out after {timeout}s")`.
   - `LOCKED` in stdout or stderr → `AddError("the library is open in another Calibre program (desktop app or
     calibre-server); close it and retry")`.
   - Non-zero exit → `AddError("calibredb failed: " + " | ".join(last 5 lines of stderr or stdout))`.
   - Otherwise return stdout.
6. Return `parse_output(stdout)`, run on stdout only:
   - `^Added book ids: (.+)$` → `added` with ints split on `,`.
   - `^Merged book ids: (.+)$` → `merged`.
   - The `already exist in the database` header → `duplicate`. Each subsequent line indented by exactly 2 spaces
     (not 4) is a title.
   - If both added and merged ids appear, the status is `added` and `book_ids` holds both lists. The guards against
     getting there are a single file per call and `--automerge ignore`.
   - Anything else → `AddError("calibredb added nothing: " + tail)`.
7. Log with `log.info("adding %s to %s", src.name, library_root)`, using logger `calibre_mcp`.

### `server.py` (T2)

- `Catalog.__init__`: `self.importer = Importer(cfg.calibredb, cfg.path_maps, timeout=cfg.add_timeout)`. If maps are
  configured but `calibredb` is missing, `log.warning`. New property `can_add -> bool` = `available and configured`.
- `WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)`.
- New tool:

```python
@mcp.tool(annotations=WRITE)
async def add_book(path: str, ctx: Context, title: str | None = None, authors: list[str] | None = None,
                   tags: list[str] | None = None, series: str | None = None, series_index: float | None = None,
                   languages: list[str] | None = None, identifiers: dict[str, str] | None = None,
                   duplicates: Literal["refuse", "new_record", "merge"] = "refuse",
                   library: str | None = None) -> dict:
    """Add an ebook file that is already on the server to a library, using Calibre's calibredb. `path` is the
    file's absolute path as you see it; it must be inside one of the import_dirs that list_libraries reports.
    Calibre reads title/authors/etc. from the file; the optional fields override them (authors and languages
    are lists, identifiers e.g. {"isbn": "978…"}). If Calibre finds a book with the same title and authors,
    nothing is added and status is "duplicate" with the existing matches; retry with duplicates="new_record"
    to add a separate record, or "merge" to add this file as another format of the existing book."""
```

  Response shapes, all **non-error** results:
  - `{"status": "added", "library": name, "books": [<lib.book(id) + cat.links(...)> for each id]}`
  - `{"status": "merged", "library": name, "books": [...]}`, same shape, for the merged-into records.
  - `{"status": "duplicate", "library": name, "message": "already in the library: <titles>; pass
    duplicates=\"new_record\" or \"merge\"", "existing": [summaries + links]}`. `existing` holds
    `lib.search(title, limit=10)` for each reported title, deduplicated by id and capped at 10. It may be empty,
    because Calibre's duplicate matching is fuzzier than our search.
  - If `lib.book(id)` returns None for a reported id, the server skips it. If every id is skipped, the tool raises
    `ToolError("calibredb reported book ids … but they are not in metadata.db")`.
  - `AddError` → `ToolError(str(exc))`. An unknown library uses the existing `lib_or_error`.
- `list_libraries` gains `"add_books": cat.can_add, "import_dirs": cat.importer.import_dirs`. `GET /` gains
  `"add_books": cat.can_add`.
- `INSTRUCTIONS`: replace "Read-only access to …" with wording that reads stay read-only, and add one sentence on
  `add_book` and `import_dirs`. Update the module docstring.

### `__main__.py` (T2)

- `--path-map FROM=TO` (`action="append"`). When given, it replaces `cfg.path_maps` with
  `[*parse_path_maps(s) for s in args.path_map]`.
- `--calibredb PATH`.
- `check` prints `add books: yes|no (calibredb: found|missing; import dirs: a → b, …)`.

### Conventions

- Match the existing style: `from __future__ import annotations`, dataclasses, 120-column lines, short docstrings,
  logger `calibre_mcp`, blocking subprocess work in `asyncio.to_thread`, and user-facing errors as one exception
  class that becomes `ToolError` (as with `ConvertError`).
- Never pass user strings through a shell. Always use an argv list and `--` before the path.
- Tests use stdlib `unittest` only. Fake external binaries are executable scripts in a temp dir (like
  `fake_converter`). For Python fakes, use the shebang `#!{sys.executable}`.

### Docs (T2)

README:
- New "Adding books" section covering `add_book`, path maps (TOML, env and flag examples), the scope rule, duplicate
  modes, and the requirement for calibredb plus a **read-write** library mount.
- The warning: don't add while the Calibre desktop app has the same library open. Containers can't detect it.
- Config table and TOML example updated.
- Docker compose example: library mount becomes rw, plus `- /srv/inbox:/import:ro` and
  `CALIBRE_MCP_PATH_MAPS: /srv/inbox=/import`.
- Intro sentence: reads are read-only; adds go through calibredb.

Dockerfile: comment only (`WITH_CALIBRE=0` also disables `add_book`).

## Tasks

### Wave 1

**T1: Path-mapped calibredb importer and its config**
- Goal: everything needed to turn `(library root, client path, metadata, duplicates)` into an `AddResult`, safely.
  No MCP code.
- Owned files: `src/calibre_mcp/config.py`, `src/calibre_mcp/importer.py` (new), `tests/test_importer.py` (new).
- Produces: `PathMap`, `Config.path_maps / calibredb / add_timeout`, `parse_path_maps`, and `importer.{Duplicates,
  AddError, BookMetadata, AddResult, translate, parse_output, Importer}`, exactly as specified above.
- Consumes: nothing new.
- Dependencies: none.
- Tests (`tests/test_importer.py`):
  - config: TOML `[[path_maps]]`, env override, `parse_path_maps` error, and `validate()` rejecting a relative
    source/target and a missing target dir.
  - `translate`: mapped, longest-prefix, already-under-target, prefix without a component boundary
    (`/host/inbox2` vs `/host/inbox`), `..` escape, symlink escape, missing file, directory, no extension, and a
    relative path.
  - `BookMetadata.args`: every field, plus each invalid case.
  - `parse_output`: added, merged (several ids), duplicate (titles), stderr-noise independence, and garbage.
  - `Importer.add` with a fake calibredb Python script that writes its argv as JSON to a file and prints canned
    stdout/stderr/exit code chosen through a file. Covers the flags per duplicates mode, the `--` placement, the
    lock message, non-zero exit, timeout (fake sleeps; `timeout=1`), not available, and not configured.
  - A real-calibredb test, `skipUnless(shutil.which("calibredb"))`, with `skipTest` when stdout/stderr has `LOCKED`.
    Bootstrap a library with `calibredb --with-library d list`, add a `.txt` with title/authors → `added`; repeat →
    `duplicate`; `new_record` → `added` with a different id.
- Unit test command: `nix develop -c python -m unittest discover -s tests -p 'test_importer.py' -v`

### Wave 2 (after T1 is merged into the branch)

**T2: `add_book` MCP tool, CLI flags and docs**
- Goal: expose the importer over MCP, the CLI and the README.
- Owned files: `src/calibre_mcp/server.py`, `src/calibre_mcp/__main__.py`, `tests/test_calibre_mcp.py`,
  `README.md`, `Dockerfile` (comment only).
- Consumes: T1's interfaces. Produces: the `add_book` tool, `list_libraries.add_books/import_dirs`,
  `GET /`.add_books, and the `--path-map/--calibredb` flags.
- Dependencies: T1.
- Tests (`tests/test_calibre_mcp.py`):
  - Update the expected tool set to 5 tools.
  - A fake calibredb (Python script) that, on `add`, inserts a book into the synthetic `metadata.db` (books, data,
    authors, links) and copies the file into `<root>/<author>/<title> (<id>)/`. It prints `Added book ids: N`. If
    the title is already in `books` and `--duplicates` is absent, it prints the duplicate block instead.
  - MCP tests with `path_maps` pointing at a temp inbox dir: add via a client path → `added`, the book is returned
    with `downloads`/`epub_url`, and `search_books` finds it. Then a duplicate → `duplicate` with `existing`, then
    `new_record` → added. An outside path → error. An unconfigured app (no maps) → error, and
    `list_libraries.add_books == False`.
  - Keep the existing tests passing. Give the add tests their own library copy and app so the existing ids and
    counts (3 books) are untouched.
  - `__main__`: `main(["check", "-l", ..., "--path-map", "/a=<dir>"])` output contains `add books:`.
- Unit test command: `nix develop -c python -m unittest discover -s tests -v`

The two tasks run in separate waves because T2 imports T1's modules.

## Test and verification

| Kind | Command |
| --- | --- |
| Unit, all | `nix develop -c python -m unittest discover -s tests -v` |
| Unit, T1 only | `nix develop -c python -m unittest discover -s tests -p 'test_importer.py' -v` |
| Real calibredb (local) | The real-calibredb test skips itself while a desktop Calibre is running for this user. To force it: `unshare -rn nix develop -c python -m unittest discover -s tests -p 'test_importer.py' -v` |
| Syntax/bytecode | `nix develop -c python -m compileall -q src tests` |
| Lint/type | None configured (no ruff/mypy in the repo). Don't add tooling for this feature. |
| Nix build (runs unit tests, no calibre → real test skips) | `git add -A && nix build .#calibre-mcp --no-link` (the flake fileset only sees git-tracked files) |
| Docker build (runs unit tests **with** calibre, so the real-calibredb test runs) | `docker build -t calibre-mcp:e2e .` |

Testing policy: the agent runs the sandbox E2E below **once, now that the feature is first added**, including any
repairs and reruns it needs. Later pushes and deployments need no new sandbox E2E, but must still run the existing
unit tests (`python -m unittest discover -s tests`, also run by the nix and Docker builds). The repo has no AGENTS.md.
If one is added later, it must keep this scope.

## E2E journey (sandbox, Docker)

Setup: `E=$(mktemp -d)`, with `$E/lib`, `$E/inbox` and `$E/cache`. Run the container as the current uid
(`-u $(id -u):$(id -g)`). Use `curl` against `/mcp` with JSON-RPC (stateless, `json_response`), or the Python MCP
client from `nix develop`. Either is fine; record the commands and outputs in the E2E report.

1. `docker build -t calibre-mcp:e2e .` → the build succeeds, and the unit tests run inside it, including the
   real-calibredb test.
2. Bootstrap an empty library:
   `docker run --rm -u … -v $E/lib:/libraries/main --entrypoint calibredb calibre-mcp:e2e --with-library /libraries/main list`
   → `$E/lib/metadata.db` exists.
3. Make a real EPUB in the inbox. Use `ebook-convert` from the image on a small `.txt`, passing
   `--title "E2E Book" --authors "Test Author"`, or any small valid EPUB, to `$E/inbox/e2e.epub`.
4. Start the server: `docker run -d -p 18080:8000 -u … -v $E/lib:/libraries/main -v $E/inbox:/import:ro
   -v $E/cache:/cache -e CALIBRE_MCP_LIBRARIES=main=/libraries/main -e CALIBRE_MCP_PATH_MAPS=$E/inbox=/import
   calibre-mcp:e2e` → `GET /health` returns `{"ok": true}`, and `GET /` shows `"add_books": true`.
5. MCP `tools/list` → contains `add_book`. `list_libraries` → `add_books: true`, `import_dirs: ["$E/inbox"]`,
   `books: 0`.
6. `add_book(path="$E/inbox/e2e.epub")` (host path) → `status: "added"`, and `books[0].title == "E2E Book"`
   (embedded metadata), `authors == ["Test Author"]`, and `downloads.EPUB` is present.
7. `search_books(query="E2E")` → total 1, same id. `curl -fJO <downloads.EPUB>` → 200 with a non-empty EPUB. On the
   host, `$E/lib/Test Author/E2E Book (<id>)/` contains the epub and `metadata.opf`.
8. `add_book` with the same path again → `status: "duplicate"`, `existing` contains the book id from step 6, and
   `list_libraries.books` is still 1.
9. `add_book(path=…, duplicates="new_record", title="E2E Book Copy", tags=["e2e"], languages=["eng"],
   identifiers={"isbn": "9780000000002"})` → `added` with a new id, and `get_book` shows the overridden title,
   tags, language and identifier.
10. `add_book(path="/etc/passwd")` and `add_book(path="$E/inbox/../../etc/passwd")` → tool errors ("not inside an
    import directory"). `add_book(path="$E/inbox/missing.epub")` → tool error ("no such file"). The book count is
    unchanged.
11. Restart without `CALIBRE_MCP_PATH_MAPS`. `list_libraries.add_books` is `false`, `add_book` returns the
    "not configured" tool error, and the read tools still work.
12. Clean up: stop and remove the container, and delete `$E`.
