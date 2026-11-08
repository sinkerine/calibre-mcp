# Feature plan: add a book to a Calibre library (HTTP upload)

Status: **revision 2, approved by the user (2026-11-07)**, including implementation, review, E2E and the automatic
final merge to the default branch. It supersedes revision 1 (MCP `add_book(path)` with path
maps). Execution goal `mw7bzt2p-a494pu` stays **paused** until the user runs `/goal-resume`.
Branch `feat/add-book`, base `038d746`.

## Goal and acceptance criteria

Any HTTP client can add an ebook to a configured Calibre library by POSTing the file to
`POST /<library>/books`. MCP agents learn the upload URL from `list_libraries` and use it with `curl`. The server
saves the upload to a temporary file and imports it with Calibre's own `calibredb add`. Reads stay read-only
SQLite queries, as they are today.

Decisions made with the user:

| Topic | Decision |
| --- | --- |
| Input | The file itself, uploaded over HTTP POST. No server paths, path maps, URLs or base64. |
| Body formats | `multipart/form-data` (a `file` part plus optional metadata fields) **and** a raw body (`--data-binary`) with `filename` and metadata in query params. |
| MCP | No MCP write tool. `list_libraries` and `INSTRUCTIONS` advertise the upload URL. The MCP tool set stays at the existing 4 tools. |
| Write mechanism | `calibredb add --with-library <root>` subprocess. No direct SQLite writes. |
| Duplicates | Default `refuse` returns the existing matches. The caller can opt in to `new_record` (`--duplicates`) or `merge` (`--automerge ignore`). |
| Enablement | Always on whenever `calibredb` is installed. No auth (the proxy decides who can reach it). |
| Size cap | `max_upload_mb`, default **1024** (1 GiB). Above it → 413. |
| Extras | Optional metadata overrides: title, authors, tags, series, series_index, languages, identifiers. |

Acceptance criteria (each one is checked by a unit test or an E2E step):

1. `curl -F file=@x.epub http://host/main/books` → **201**, with header `Location: <base>/main/books/<id>`, body
   `{"status": "added", "library": "main", "books": [<get_book JSON + links>]}`. The book then appears in
   `search_books`, and `GET /main/books/<id>/epub` returns the uploaded bytes.
2. `curl --data-binary @x.epub 'http://host/main/books?filename=x.epub'` (any non-multipart Content-Type,
   including curl's default `application/x-www-form-urlencoded`) behaves the same as criterion 1.
3. Metadata, whether as multipart form fields or as query params (raw mode), reaches `calibredb`. The fields are
   `title`, `authors` (`A & B`, or the field repeated), `tags` and `languages` (comma-separated, or repeated),
   `series`, `series_index`, and `identifier` (repeated, `type:value`, e.g. `isbn:978…`). Invalid values → **400**
   before `calibredb` runs.
4. The same title and authors again with the default `duplicates=refuse` → **409**, body
   `{"status": "duplicate", "message": …, "existing": [...]}`, and no record is added. `duplicates=new_record` →
   201 with a new id. `duplicates=merge` → **200** `{"status": "merged", "books": [...]}`, or 409 when that format
   already exists. An unknown `duplicates` value → 400.
5. Body larger than `max_upload_mb` → **413**, whether declared via `Content-Length` (rejected before reading) or
   streamed/chunked (aborted once the cap is reached). No temp file is left behind and the library is unchanged.
6. A missing `file` part, an empty body, a missing or extensionless filename, or more than one file part → **400**.
   Unknown library → **404**. All error bodies are `{"error": "<message>"}`.
7. `calibredb` not installed → **503** "calibredb is not installed…". Library locked by another Calibre program →
   **503** with a clear message. `calibredb` timeout, non-zero exit, or output it can't parse → **502** with the
   tail of stderr.
8. Cross-site guard: a request whose `Origin` header doesn't match the request host (or `allowed_hosts`, when set) →
   **403**. Requests without `Origin` (curl, scripts) are allowed.
9. `list_libraries` reports `add_books` (bool) and, per library, `add_url` when adding is possible. `GET /` reports
   `add_books` and the POST endpoint. `calibre-mcp check` prints add-books status. The MCP tool set is unchanged at
   4 tools.
10. Config: TOML `calibredb`, `add_timeout`, `max_upload_mb`; env `CALIBRE_MCP_CALIBREDB`,
    `CALIBRE_MCP_MAX_UPLOAD_MB`; flags `--calibredb`, `--max-upload-mb`. Path-map config from revision 1 is
    removed.
11. Temp uploads live under `<cache_dir>/uploads/` and are always removed after the request, on success or on
    error. Leftovers from a crash are swept when the app starts.
12. All existing tests still pass, and the README documents the endpoint, including the rw library mount.

Non-goals: an MCP write tool, path or URL import, adding a format to a chosen book id, cover upload, auth, and
upload progress or resumable uploads.

## Design

### What `calibredb add` actually does (probed with calibre 9.17)

- On success, stdout has `Added book ids: 4`. With `--automerge`, stdout has `Merged book ids: 1, 2`, and merges
  can touch **several** existing records.
- When it refuses a duplicate, stdout has `The following books were not added as they already exist in the database
  (see --duplicates option or --automerge option):`, then for each book a line with the title (2-space indent) and
  a line with the path (4-space indent). **Exit code 0.**
- A missing file prints `<path> not found` and an extensionless file prints nothing, both with **exit code 0**. So
  we pre-validate the file and treat "nothing recognised" as an error.
- Another Calibre program (GUI or calibre-server) running for the same user/network namespace → `Another calibre
  program such as calibre-server or the main calibre program is running…`, exit 1. Containers have their own
  namespace, so the check does **not** see a desktop Calibre on the host. The README warns against adding while the
  desktop app has the same library open.
- Format readers write tracebacks to stderr even on success (e.g. a bad PDF info dict). Parse **stdout** only.
- When the file has no embedded metadata (e.g. `.txt`), Calibre derives title and author from the **file name**.
  So the upload keeps the client's (sanitised) file name.
- `calibredb --with-library <root> list` on an empty dir creates a fresh `metadata.db` (used by tests and E2E).
- Option order is flexible, and `--` before the file path works.

### Module boundaries

```
config.py    Config.calibredb / add_timeout / max_upload_mb; env vars; (path maps removed)      [T1]
importer.py  calibredb runner: BookMetadata → args, Importer.add(root, file), output parsing,
             AddError with an HTTP-ish status. No Starlette/MCP imports, no path translation.   [T1]
upload.py    NEW: HTTP request → (temp file, BookMetadata, duplicates). Body-size cap, multipart
             and raw parsing, filename sanitising, temp-dir lifecycle. Starlette only.          [T2]
server.py    POST /{library}/books route, Origin guard, response shaping, Catalog.importer /
             can_add, list_libraries + index fields, INSTRUCTIONS                               [T2]
__main__.py  --calibredb, --max-upload-mb flags; `check` prints add status                      [T2]
library.py   unchanged (the new book is read back through Library.book())
```

Data flow: `POST /{library}/books` → Origin guard → `cat.libraries[name]` (404) → `upload.receive(request,
upload_root, max_bytes)`, an async context manager that yields an `Upload(path, metadata, duplicates)` and deletes
its temp dir on exit → `cat.importer.add(lib.root, upload.path, upload.metadata, upload.duplicates)` → per-library
`asyncio.Lock` → `asyncio.to_thread(_run)` → `calibredb` subprocess → `parse_output()` → `AddResult`. The route then
uses `Library.book(id)` (fresh read-only connection) and `Catalog.links()` to build a JSON response with the status
code.

### Reuse of the existing uncommitted T1 work

The worktree holds uncommitted revision-1 T1 changes: `config.py`, `importer.py` and `tests/test_importer.py`. T1 in
this revision **adapts** them rather than starting over. It keeps `BookMetadata`, `AddResult`, `parse_output`,
`Importer.command`, `Importer._run` and the lock/timeout/exit handling with their tests. It removes `PathMap`,
`parse_path_maps`, `translate`, the `path_maps` field, `Importer.configured` and `Importer.import_dirs`, along with
their tests.

### `config.py` (T1)

```python
@dataclass
class Config:
    ...existing fields (unchanged)...
    calibredb: str = "calibredb"
    add_timeout: int = 300
    max_upload_mb: int = 1024
```

- `load_config`: TOML `calibredb`, `add_timeout`, `max_upload_mb`; env `CALIBRE_MCP_CALIBREDB`,
  `CALIBRE_MCP_MAX_UPLOAD_MB` (int).
- `validate()`: require `max_upload_mb >= 1` and `add_timeout >= 1`, otherwise `SystemExit`.
- Remove `PathMap`, `parse_path_maps`, `path_maps`, `CALIBRE_MCP_PATH_MAPS`, and the path-map docstring lines.
  Update the docstring with the new keys.

### `importer.py` (T1)

```python
from typing import Literal

Duplicates = Literal["refuse", "new_record", "merge"]
DUPLICATES: tuple[str, ...] = ("refuse", "new_record", "merge")
LOCKED = "Another calibre program"

class AddError(RuntimeError):
    """A user-facing reason the book was not added."""
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status   # 400 bad input, 503 unavailable/locked, 502 calibredb failed

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
        """calibredb add options for the fields that are set; raises AddError(…, 400) on invalid values."""

@dataclass
class AddResult:
    status: Literal["added", "merged", "duplicate"]
    book_ids: list[int]           # added or merged ids; [] for "duplicate"
    duplicate_titles: list[str]   # titles calibredb reported as already in the library

def parse_output(stdout: str) -> AddResult:
    """Interpret calibredb add stdout; raises AddError(…, 502) when nothing recognisable was reported."""

class Importer:
    def __init__(self, exe: str, *, timeout: int = 300) -> None: ...
    @property
    def available(self) -> bool: ...        # shutil.which(exe) found
    def command(self, library_root: Path, src: Path, metadata: BookMetadata,
                duplicates: Duplicates = "refuse") -> list[str]: ...
    async def add(self, library_root: Path, src: Path, metadata: BookMetadata,
                  duplicates: Duplicates = "refuse") -> AddResult: ...
```

`BookMetadata.args()` (the rules carried over from revision 1). Strip every string; an empty string after stripping
counts as not set, except `title`, where an explicit empty title raises an error.
- `title` → `["--title", t]`
- `authors` → `["--authors", " & ".join(a)]`. An element containing `&` or empty → `AddError`.
- `tags` → `["--tags", ",".join(t)]`. An element containing `,` → `AddError`.
- `series` → `["--series", s]`. `series_index` → `["--series-index", repr(float)]`, and it needs `series`.
- `languages` → `["--languages", ",".join(l)]`. An element containing `,` → `AddError`.
- `identifiers` → one `["--identifier", f"{k.lower()}:{v}"]` per item, in sorted key order. The key must match
  `^[A-Za-z0-9_-]+$`, and the value must be non-empty with no `,`.

`Importer.command()` returns:
`[exe, "add", "--with-library", str(library_root), *dup_flags, *metadata.args(), "--", str(src)]`, where
`dup_flags` is `[]` for refuse, `["--duplicates"]` for new_record, and `["--automerge", "ignore"]` for merge. An
unknown `duplicates` value → `AddError(…, 400)`.

`Importer.add()` steps:
1. `not available` → `AddError("calibredb is not installed; adding books needs Calibre", 503)`.
2. `src` must be an existing regular file with a non-empty suffix → `AddError(…, 400)`. Then
   `cmd = self.command(...)`, so validation runs before any subprocess.
3. Take the per-library-root `asyncio.Lock`, then run `await asyncio.to_thread(self._run, cmd)`.
4. `_run(cmd) -> str` runs `subprocess.run(cmd, capture_output=True, text=True, timeout=…, env=…)` inside
   `tempfile.TemporaryDirectory(prefix="calibre-mcp-add-")`, with
   `env = dict(os.environ, HOME=tmp, QT_QPA_PLATFORM="offscreen")`.
   - `TimeoutExpired` → `AddError("calibredb timed out after Ns", 502)`.
   - `LOCKED` in stdout or stderr → `AddError("the library is open in another Calibre program (desktop app or
     calibre-server); close it and retry", 503)`.
   - Non-zero exit → `AddError("calibredb failed: " + tail of last 5 lines, 502)`.
   - Otherwise return stdout.
5. Return `parse_output(stdout)`, run on stdout only:
   - `^Added book ids: (.+)$` → `added`.
   - `^Merged book ids: (.+)$` → `merged`.
   - The `already exist in the database` header → `duplicate`. Each subsequent line indented by exactly 2 spaces is
     a title.
   - Anything else → `AddError("calibredb added nothing: " + tail, 502)`.

### `upload.py` (T2, new)

```python
@dataclass
class Upload:
    path: Path                 # temp file named after the sanitised client filename
    metadata: BookMetadata
    duplicates: Duplicates

class UploadError(Exception):
    def __init__(self, message: str, status: int = 400) -> None: ...   # 400 / 413

def safe_filename(name: str) -> str:
    """Basename only (split on / and \\); drop control chars and \\ / : * ? " < > |; strip spaces and dots;
    cap the stem at 150 chars keeping the extension; require a non-empty stem and extension, else UploadError."""

def metadata_from(fields: Mapping[str, list[str]]) -> tuple[BookMetadata, Duplicates]:
    """Build overrides from form fields / query params (multi-valued). authors: each value split on "&";
    tags, languages: each value split on ","; identifier: each "type:value"; series_index: float;
    duplicates: one of DUPLICATES (default "refuse"). Unknown field names are ignored. Bad values → UploadError(400)."""

@asynccontextmanager
async def receive(request: Request, upload_root: Path, max_bytes: int) -> AsyncIterator[Upload]:
    """Read the request body into a fresh temp dir under upload_root and yield an Upload; the temp dir is
    removed on exit (success or error)."""

def sweep(upload_root: Path) -> None:
    """Remove leftover ".upload-*" temp dirs (crash leftovers); called once at app start."""
```

`receive()` steps:
1. If `Content-Length` > `max_bytes`, raise `UploadError("upload larger than N MB", 413)` before reading.
2. Wrap `request.stream()` in an async generator that counts bytes and raises `UploadError(…, 413)` once
   `max_bytes` is exceeded. This also covers chunked bodies.
3. `upload_root.mkdir(parents=True, exist_ok=True)`, then `tmp = tempfile.mkdtemp(prefix=".upload-",
   dir=upload_root)`.
4. **Multipart** (`Content-Type` starts with `multipart/form-data`): build
   `starlette.formparsers.MultiPartParser(request.headers, capped_stream, max_files=1, max_fields=50)` and
   `await parser.parse()`. A `MultiPartException` becomes `UploadError(400)`.
   - Take the file from field `file`. It must be an `UploadFile`; missing → 400.
   - The filename comes from the part's filename, or the `filename` form field if given.
   - Copy the part into `tmp/<safe_filename>` in 1 MiB chunks, then close the `UploadFile`.
   - The remaining fields are metadata (`FormData.multi_items()`).
   - Note: Starlette first spools the part to the system temp dir, so a large file is written twice. This is
     acceptable at this scale.
5. **Raw** (any other Content-Type): `filename` is a required query param, falling back to a
   `Content-Disposition: …; filename=` request header. Stream the capped body straight to `tmp/<safe_filename>`;
   0 bytes → 400. Metadata comes from the query params (`request.query_params.multi_items()`).
6. Yield `Upload(...)`. In `finally`, run `shutil.rmtree(tmp, ignore_errors=True)`, which also runs on 413 or
   parse errors.

Starlette's multipart support needs `python-multipart`. It is already installed as a dependency of `mcp`, but T2
adds it explicitly: `pyproject.toml` `dependencies += ["python-multipart"]` and `flake.nix` `deps +=
python-multipart`.

### `server.py` (T2)

- `Catalog.__init__`: `self.importer = Importer(cfg.calibredb, timeout=cfg.add_timeout)`,
  `self.upload_root = cfg.cache_dir / "uploads"`, `self.max_upload = cfg.max_upload_mb * 1024 * 1024`. Property
  `can_add -> bool` = `importer.available`. Log at info level whether adding is enabled.
- `build_app` calls `upload.sweep(cat.upload_root)` once.
- Route:

```python
@mcp.custom_route("/{library}/books", methods=["POST"])
async def add_book(request: Request) -> Response:
    """Upload a book file (multipart `file` field, or raw body + ?filename=) and add it with calibredb."""
```

  Order of checks:
  1. Origin guard (`same_origin(request, cfg.allowed_hosts) -> bool`). When `Origin` is present, its host[:port]
     must equal `X-Forwarded-Host` or `Host`, or be in `allowed_hosts` when set. Otherwise → 403
     `{"error": "cross-origin upload refused"}`.
  2. Unknown library → 404.
  3. `not cat.can_add` → 503, checked before reading the body.
  4. `async with upload.receive(...) as up: result = await cat.importer.add(lib.root, up.path, up.metadata,
     up.duplicates)`.

  Responses:
  - `added` → **201**, `Location: <base>/<lib>/books/<first id>`,
    `{"status": "added", "library", "books": [lib.book(id) + cat.links(...)]}`.
  - `merged` → **200**, same shape, with `"status": "merged"`.
  - `duplicate` → **409**, `{"status": "duplicate", "library", "message": "already in the library: <titles>; send
    duplicates=new_record or duplicates=merge", "existing": [...]}`. `existing` holds `lib.search(title, limit=10)`
    summaries plus links for each reported title, deduplicated by id and capped at 10. It may be empty.
  - If `lib.book(id)` returns None for a reported id, the route skips that id. If every id is skipped → 502.
  - `UploadError` / `AddError` → `{"error": str(exc)}` with `exc.status`.
- `list_libraries(ctx: Context)` gains `"add_books": cat.can_add`, and each library entry gains
  `"add_url": f"{base}/{name}/books"` when `can_add`. `base` comes from `cat.base_url(ctx.headers)`, as in the
  other tools.
- `GET /` gains `"add_books": cat.can_add, "add": "POST /<library>/books"`.
- `INSTRUCTIONS`: replace "Read-only access …" with wording that the tools are read-only, and add: "To add a book,
  POST the file to the library's `add_url` (e.g. `curl -F file=@book.epub <add_url>`; optional fields title,
  authors, tags, series, series_index, languages, identifier=type:value, duplicates=refuse|new_record|merge)."
  Update the module docstring's route list.

### `__main__.py` (T2)

- `--calibredb PATH`, `--max-upload-mb N`.
- `check` prints `add books: yes|no (calibredb: <path>|missing; max upload: N MB)`.

### Conventions

- Match the existing style: `from __future__ import annotations`, dataclasses, 120-column lines, short docstrings,
  logger `calibre_mcp`, blocking subprocess work in `asyncio.to_thread`, and JSON error bodies `{"error": …}` as in
  the existing routes.
- Never pass user strings through a shell. Always use an argv list and `--` before the path. The user's filename is
  only ever a sanitised basename inside our own temp dir.
- Tests use stdlib `unittest` and Starlette's `TestClient`. Fake external binaries are executable scripts in a temp
  dir. For Python fakes, use the shebang `#!{sys.executable}`.

### Docs (T2)

README:
- New "Adding books" section: the endpoint, curl examples (multipart and raw), fields, status codes, duplicate modes,
  `max_upload_mb`, and the requirement for calibredb plus a **read-write** library mount.
- Warnings: there's no auth, so restrict POST at the proxy if needed; don't add while the Calibre desktop app has the
  same library open.
- Config table and TOML example updated. Docker compose example: the library mount becomes rw, and a note that a
  proxy body-size limit (e.g. Traefik buffering) must allow the size.
- Intro sentence: reads are read-only; adds go through calibredb.

Dockerfile: comment only (`WITH_CALIBRE=0` also disables adding).

## Tasks

### Wave 1

**T1: calibredb importer and its config (adapt the existing uncommitted work)**
- Goal: turn `(library root, local file, BookMetadata, duplicates)` into an `AddResult` safely, with status-carrying
  errors. No HTTP code. Strip out revision 1's path-map pieces.
- Owned files: `src/calibre_mcp/config.py`, `src/calibre_mcp/importer.py`, `tests/test_importer.py`.
- Produces: `Config.calibredb / add_timeout / max_upload_mb`, and `importer.{Duplicates, DUPLICATES, AddError(status),
  BookMetadata, AddResult, parse_output, Importer}`, as specified above.
- Consumes: nothing new.
- Dependencies: none.
- Tests (`tests/test_importer.py`):
  - config: TOML and env for the new keys, `validate()` limits, and confirming that path-map config is gone.
  - `BookMetadata.args`: every field, plus each invalid case.
  - `parse_output`: added, merged (several ids), duplicate (titles), stderr-noise independence, and garbage (502).
  - `Importer.add` with a fake calibredb Python script that writes its argv as JSON to a file and prints canned
    output and an exit code chosen through a file. Covers the flags per duplicates mode, the `--` placement, a
    missing or extensionless src (400, fake not run), the lock message (503), non-zero exit (502), timeout (502,
    `timeout=1`), and not available (503).
  - A real-calibredb test, `skipUnless(shutil.which("calibredb"))`, with `skipTest` when `LOCKED` appears. Bootstrap
    a library with `calibredb --with-library d list`, add a `.txt` with title/authors → `added`; repeat →
    `duplicate`; `new_record` → `added` with a new id.
- Unit test command: `nix develop -c python -m unittest discover -s tests -p 'test_importer.py' -v`

### Wave 2 (after T1 is in the branch)

**T2: HTTP upload endpoint, MCP discovery, CLI and docs**
- Goal: accept uploads at `POST /{library}/books` and wire them to the importer. Advertise the endpoint to MCP
  clients and the CLI, and document it.
- Owned files: `src/calibre_mcp/upload.py` (new), `src/calibre_mcp/server.py`, `src/calibre_mcp/__main__.py`,
  `tests/test_upload.py` (new), `tests/test_calibre_mcp.py`, `README.md`, `Dockerfile` (comment only),
  `pyproject.toml`, `flake.nix`.
- Consumes: T1's interfaces. Produces: the `POST /{library}/books` route, `upload.{Upload, UploadError,
  safe_filename, metadata_from, receive, sweep}`, `list_libraries.add_books/add_url`, `GET /`.add_books, and the
  `--calibredb/--max-upload-mb` flags.
- Dependencies: T1.
- Tests:
  - `tests/test_upload.py`: `safe_filename` (paths, `..`, control chars, unicode kept, long names, no extension),
    `metadata_from` (splitting, repeated fields, identifiers, bad series_index or duplicates, unknown fields
    ignored), and `receive` through a tiny Starlette app with TestClient. Covers multipart, raw with
    `?filename=` / Content-Disposition, missing file part, empty body, Content-Length > cap (413), a chunked body
    over the cap (413, via a generator body), and the temp dir removed after success and after errors. Plus
    `sweep`.
  - `tests/test_calibre_mcp.py`: a fake calibredb (Python script) that, on `add`, inserts a book into the synthetic
    `metadata.db` and copies the file to `<root>/<author>/<title> (<id>)/<title> - <author>.<ext>`. It prints
    `Added book ids: N`, or prints the duplicate block when the title already exists and `--duplicates` is absent.
    The add tests use their own library copy and app, so the existing 3-book assertions stay untouched.
    - Multipart upload → 201 + Location, and the book is downloadable.
    - Raw upload → 201.
    - Duplicate → 409 with `existing`; `new_record` → 201.
    - Bad metadata → 400. Unknown library → 404. Calibredb missing → 503.
    - Foreign Origin → 403; matching Origin → allowed.
    - `list_libraries` shows `add_books` / `add_url`, and the tool set is still the 4 existing tools.
  - `__main__`: `check` output contains `add books:`.
- Unit test command: `nix develop -c python -m unittest discover -s tests -v`

The two tasks run in separate waves because T2 imports T1's modules and T1 edits `config.py`, which T2's tests load.

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

Setup: `E=$(mktemp -d)`, with `$E/lib` and `$E/cache`. Run the container as the current uid
(`-u $(id -u):$(id -g)`). Use `curl` for HTTP. Use JSON-RPC over `curl` to `/mcp`, or the Python MCP client from
`nix develop`, for the MCP checks. Record the commands and outputs in the E2E report.

1. `docker build -t calibre-mcp:e2e .` → the build succeeds, and the unit tests run inside it, including the
   real-calibredb test.
2. Bootstrap an empty library:
   `docker run --rm -u … -v $E/lib:/libraries/main --entrypoint calibredb calibre-mcp:e2e --with-library /libraries/main list`
   → `$E/lib/metadata.db` exists.
3. Make a real EPUB: run `ebook-convert` from the image on a small `.txt` with
   `--title "E2E Book" --authors "Test Author"`, to `$E/e2e.epub`.
4. Start the server: `docker run -d -p 18080:8000 -u … -v $E/lib:/libraries/main -v $E/cache:/cache
   -e CALIBRE_MCP_LIBRARIES=main=/libraries/main -e CALIBRE_MCP_MAX_UPLOAD_MB=5 calibre-mcp:e2e` →
   `GET /health` returns `{"ok": true}`, and `GET /` shows `"add_books": true`.
5. MCP `list_libraries` → `add_books: true`, `add_url: "http://127.0.0.1:18080/main/books"`, `books: 0`. `tools/list`
   still has exactly the 4 read tools.
6. `curl -sS -F file=@$E/e2e.epub http://127.0.0.1:18080/main/books -D -` → 201, a `Location` header, and
   `books[0].title == "E2E Book"`, `authors == ["Test Author"]`, with `downloads.EPUB` present.
7. MCP `search_books(query="E2E")` → total 1, same id. `curl -f <downloads.EPUB> | cmp - $E/e2e.epub` → identical.
   On the host, `$E/lib/Test Author/E2E Book (<id>)/` contains the epub and `metadata.opf`.
8. The same multipart upload again → 409 `status: "duplicate"` with `existing[0].id` equal to the id from step 6, and
   `list_libraries.books` is still 1.
9. Raw upload: `curl --data-binary @$E/e2e.epub 'http://127.0.0.1:18080/main/books?filename=e2e.epub&duplicates=new_record&title=E2E%20Copy&tags=e2e,test&languages=eng&identifier=isbn:9780000000002'`
   → 201 with a new id. `get_book` shows the overridden title, tags, language and identifier.
10. Errors, with the book count unchanged after each:
    - a 6 MB random `x.epub` → 413;
    - multipart without a `file` part → 400;
    - raw upload without `filename` → 400;
    - `POST /nope/books` → 404;
    - `-H 'Origin: http://evil.example'` → 403.
11. `$E/cache/uploads/` is empty (no leftover temp dirs).
12. Run the image with `--build-arg WITH_CALIBRE=0`, or start the container with
    `-e CALIBRE_MCP_CALIBREDB=/nonexistent`. `list_libraries.add_books` is `false`, POST returns 503, and the read
    endpoints still work.
13. Clean up: stop and remove the container, and delete `$E`.
