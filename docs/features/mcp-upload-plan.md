# Feature plan: MCP-coordinated book upload (prepare_upload → PUT → add_book)

Status: **revision 2, approved by the user (2026-11-08)**. It includes the user's answers on the POST endpoint,
duplicate reporting, persistence and the metadata shape.
Branch `feat/mcp-upload`, base `main` @ `d7c61bc`.

## Goal and acceptance criteria

An MCP client that also has a shell adds a book in three steps:

1. MCP `prepare_upload(filename)` returns an expiring `upload_url`, an opaque `file_id` and the HTTP method.
2. The shell uploads the bytes with `curl` (`curl --fail-with-body -T ./book.epub '<upload_url>'`).
3. MCP `add_book(file_id, library?, <metadata>?, duplicates="refuse")` imports the staged file through the
   existing `Importer.add` and returns structured metadata plus download links.

The bytes never go through MCP, and there are no server paths, base64 or URL fetches. If an import is refused (a
duplicate, a validation error, a locked library or a calibredb failure), the staged file is kept so the client can
call `add_book` again without uploading again. A successful import consumes the file. Abandoned files expire. The
one-shot `POST /<library>/books` endpoint is **removed**; the staged flow is the only way to add books.

Constraints (given by the user, fixed):

| Topic | Constraint |
| --- | --- |
| MCP SDK | Keep the official `mcp` SDK (`mcp>=2.3,<3`, `mcp.server.mcpserver.MCPServer`). No FastMCP and no new dependency. |
| mcp-upload | Ideas only; no dependency on it. |
| Staging | Our own small staging layer on local disk under `<cache_dir>/uploads`. No S3, Redis or upload framework. |
| Import | Reuse `Importer.add`, `BookMetadata` validation and `parse_output` duplicate detection unchanged. |

Decisions:

| Topic | Decision |
| --- | --- |
| Old `POST /<library>/books` | **Removed** (user decision). This removes the route, `upload.receive`, `metadata_from`, `upload.py` itself and their tests. Afterwards `POST /main/books` → 404/405 from Starlette routing (no route). |
| Duplicate result | **A normal structured result** `status: "duplicate"` (not `isError`) with `existing` books, and the `file_id` stays valid (user decision). Other refusals are `ToolError`. |
| Persistence | **An in-memory registry** with the bytes on disk (user decision). A restart drops staged files: the startup sweep removes them. A single uvicorn process, as `__main__` runs today. |
| Metadata shape | **Flat keyword parameters** (`title`, `authors`, `tags`, `series`, `series_index`, `languages`, `identifiers`), like `search_books` (user decision). |
| Upload method | `PUT <base>/uploads/<file_id>` with a raw body (`curl -T`). No multipart. |
| Token | `file_id` = `secrets.token_urlsafe(24)` (32 chars, 192 bits). It is the only capability, and `upload_url` embeds it. The service has no auth, as today. |
| Library choice | At `add_book` time. One staged file can be retried into another library. |
| Expiry | Sliding TTL `upload_ttl`, default 3600 s, counted from the last activity (prepare, successful PUT, failed `add_book`). |
| Staged cap | At most `max_staged_uploads` live entries, default 16. Worst-case disk use = cap × `max_upload_mb`. |
| Deps | `python-multipart` is dropped from `pyproject.toml` and from `flake.nix` `deps`, because nothing imports it any more (`mcp` still pulls it in transitively). `mcp>=2.3,<3`, `starlette` and `uvicorn` are unchanged. |
| Tool annotations | `prepare_upload` and `add_book`: `read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False`. The four existing tools stay `READ`. |

Acceptance criteria (each one is covered by a unit test):

1. `tools/list` returns exactly `list_libraries`, `search_books`, `get_book`, `browse_library`, `prepare_upload` and
   `add_book`. The existing four keep `read_only_hint=True`. The new two have `read_only_hint=False`.
2. `prepare_upload(filename="../My Book.epub")` returns `{"file_id", "upload_url": "<base>/uploads/<file_id>",
   "method": "PUT", "filename": "My Book.epub", "max_bytes", "expires_at": "<ISO-8601 UTC>", "expires_in":
   <int s>, "curl": "curl --fail-with-body -T <local file> '<upload_url>'"}`. `<base>` comes from
   `Catalog.base_url` (`public_url`, or the request's Host and `X-Forwarded-*` headers). A filename without a stem
   or extension → `ToolError`, and nothing is staged. Calibredb missing → `ToolError`. The cap already reached →
   `ToolError`.
3. `PUT /uploads/<file_id>` with a non-empty body → **200** `{"file_id", "filename", "size", "sha256",
   "expires_at"}`. The bytes are stored atomically (`.part`, then `os.replace`). A second PUT before import replaces
   the bytes. An unknown, expired, consumed or malformed id → **404**. An empty body → **400**. A body over
   `max_upload_mb`, declared via `Content-Length` (rejected before reading) or streamed → **413**; earlier staged
   bytes (if any) are unchanged and no `.part` remains. PUT while that id is uploading or importing → **409**. A
   foreign `Origin` → **403**. Calibredb missing → **503**. Other methods → **405**. Error bodies are
   `{"error": "<message>"}`.
4. `add_book(file_id, ...)` imports through `Importer.add(lib.root, staged.path, metadata, duplicates)`. Added →
   `{"status": "added", "library", "file_id", "books": [<get_book JSON + links>]}`. Merged → the same with
   `"status": "merged"`. The staged directory is removed, and the `file_id` is then unknown everywhere.
5. Duplicate with `duplicates="refuse"` (or merge when the format exists) → `{"status": "duplicate", "library",
   "file_id", "message", "existing": [<≤10 book JSON + links>], "expires_at"}`. The file stays staged. A retry of
   `add_book(file_id, duplicates="new_record")` succeeds without a new PUT.
6. Invalid metadata (the `BookMetadata.args()` rules, plus identifier keys that collide when lowercased) → `ToolError`
   before calibredb runs, and the file stays staged. An unknown library → `ToolError`, and the file stays staged.
   `duplicates` is a `Literal`, so the SDK rejects an unknown value during argument validation.
7. `AddError` from the importer (a locked library, a timeout or a calibredb failure) → `ToolError` with the
   importer's message. The file stays staged and its expiry is refreshed. "Calibredb reported no readable books" →
   `ToolError`, and the file is consumed (the library has already changed).
8. `add_book` on an id with no PUT yet → `ToolError` "no bytes uploaded yet for this file_id; PUT the file to
   upload_url first". On an id that is uploading or importing → `ToolError` "file_id is busy (an upload or import
   is in progress); retry shortly". On an unknown or expired id → `ToolError` "unknown or expired file_id; call
   prepare_upload again".
9. Concurrency: two simultaneous `add_book` calls on one id → exactly one `Importer.add` call. A PUT during the
   import → 409. Expiry never deletes an entry that is uploading or importing. A failed upload that ends past the
   deadline drops the entry and its directory immediately. A finished import either consumes the entry or
   releases it with a fresh deadline (a release counts as activity).
10. Expiry: expired `pending` and `ready` entries are removed by a background reaper (every `min(60, upload_ttl)` s
    while the app runs) and lazily at the start of every staging operation. At startup, every `.staged-*`
    directory and every legacy `.upload-*` directory under `<cache_dir>/uploads` is removed.
11. Config: TOML `upload_ttl` (int s, ≥ 60, default 3600) and `max_staged_uploads` (int, ≥ 1, default 16); env
    `CALIBRE_MCP_UPLOAD_TTL` and `CALIBRE_MCP_MAX_STAGED_UPLOADS`; flags `--upload-ttl` and `--max-staged-uploads`.
    `max_upload_mb` caps a PUT body. `calibre-mcp check` prints `add books: yes (calibredb: …; max upload: N MB;
    upload ttl: T s; max staged: K)`.
12. `list_libraries` keeps the top-level `add_books` bool, and each library no longer has `add_url`. `GET /`
    replaces `"add": "POST /<library>/books"` with `"upload": "MCP prepare_upload, then PUT /uploads/<file_id>,
    then MCP add_book"`. `INSTRUCTIONS` describe the three-step flow.
13. `POST /<library>/books` no longer adds anything (a non-2xx response), and `upload.py` is gone.
14. The README and SKILL.md document the staged flow. Every mention of the POST endpoint, multipart and raw-body
    upload is removed.
15. `nix develop -c python -m unittest discover -s tests -v` passes.

## Design

### Module boundaries

```
src/calibre_mcp/staging.py   NEW     Stager: registry, state machine, PUT body receive, expiry, consume, sweep;
                                     safe_filename (moved from upload.py). No MCP, no Calibre.
src/calibre_mcp/server.py    EDIT    prepare_upload / add_book tools, PUT /uploads/{file_id} route, reaper lifespan,
                                     list_libraries / INSTRUCTIONS / GET /; the POST route is removed.
src/calibre_mcp/config.py    EDIT    upload_ttl, max_staged_uploads (+ docstring).
src/calibre_mcp/__main__.py  EDIT    flags and the check output.
src/calibre_mcp/upload.py    DELETE  receive / metadata_from / sweep / Upload are replaced; safe_filename and
                                     UploadError now live in staging.py.
src/calibre_mcp/importer.py  —       unchanged.
```

### Data flow

```
agent ─MCP prepare_upload(filename)─▶ Stager.prepare(): reap; cap; safe_filename; mkdir .staged-<id>/; PENDING
agent ◀─{file_id, upload_url, method:"PUT", expires_at, expires_in, max_bytes, curl}
shell ─PUT /uploads/<id> raw bytes─▶ Stager.receive(): PENDING|READY→UPLOADING; stream→.part (cap, sha256);
                                      os.replace(.part, <filename>); →READY; refresh expiry
agent ─MCP add_book(file_id, library?, title…, duplicates)─▶ validate can_add, library, metadata (staging untouched)
                                      Stager.claim(id): READY→IMPORTING → path
                                      Importer.add(lib.root, path, metadata, duplicates)
     added|merged ─▶ Stager.consume(id): drop entry, rmtree      → {"status":"added"|"merged", books…}
     duplicate    ─▶ Stager.release(id): →READY, refresh expiry  → {"status":"duplicate", existing…}
     AddError / any exception ─▶ Stager.release(id)              → ToolError / re-raise
reaper ─every min(60, ttl) s, and at the start of each Stager op─▶ drop expired PENDING|READY entries + rmtree
```

### `staging.py` interface

```python
State = Literal["pending", "uploading", "ready", "importing"]
ID_RE = re.compile(r"[A-Za-z0-9_-]{32}")

class StagingError(Exception):
    def __init__(self, message: str, status: int = 400) -> None: ...   # .status maps to HTTP on the PUT route

def safe_filename(name: str) -> str
    # moved verbatim from upload.py; raises StagingError(400) instead of UploadError

@dataclass
class Staged:
    file_id: str
    filename: str                 # safe_filename()-ed
    dir: Path                     # <root>/.staged-<file_id>
    state: State
    expires: float                # clock() deadline
    size: int = 0
    sha256: str | None = None
    has_bytes: bool = False       # a completed PUT exists
    @property
    def path(self) -> Path        # dir / filename

class Stager:
    def __init__(self, root: Path, *, ttl: int, max_bytes: int, max_entries: int,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None
    def prepare(self, filename: str) -> Staged
        # reap(); safe_filename (400); len >= max_entries → StagingError("too many pending uploads (K); import or
        # wait for them to expire", 429); file_id = token_urlsafe(24); mkdir(parents=True); state=pending
    async def receive(self, file_id: str, request: Request, max_bytes: int | None = None) -> Staged
        # reap(); bad/unknown id → 404 "unknown or expired file_id; call prepare_upload again";
        # uploading|importing → 409 busy; Content-Length invalid → 400, > cap → 413 (before reading);
        # state=uploading; stream request.stream() to dir/".part" with a running cap (413) and sha256;
        # total == 0 → 400 "empty upload"; os.replace(.part, path); size/sha256/has_bytes set; state=ready; refresh.
        # finally on failure: unlink .part; state = ready if has_bytes else pending; if the entry is no longer
        # registered or has expired, drop it and rmtree dir.
    def claim(self, file_id: str) -> Staged
        # reap(); unknown → 404; pending → 409 "no bytes uploaded yet for this file_id; PUT the file to upload_url
        # first"; uploading|importing → 409 "file_id is busy (an upload or import is in progress); retry shortly";
        # ready → importing
    def release(self, file_id: str) -> Staged | None    # importing → ready, refresh expiry
    def consume(self, file_id: str) -> None             # drop the entry, rmtree dir (ignore_errors)
    def expires_at(self, staged: Staged) -> str         # ISO-8601 UTC "YYYY-MM-DDTHH:MM:SSZ" = wall() + (expires - clock())
    def expires_in(self, staged: Staged) -> int
    def reap(self) -> int                               # drop expired entries in pending|ready, rmtree; returns count
    def sweep(self) -> None                             # startup: rmtree root/".staged-*" and legacy root/".upload-*"
    def __len__(self) -> int
```

The `max_bytes` override on `receive` lets the route pass `cat.max_upload`, so tests can lower the cap at runtime,
as they do today.

**Concurrency model.** Everything runs on one asyncio event loop. Every state check and transition is a synchronous
section with no `await`, so it is atomic with respect to other coroutines and needs no lock. The only awaits are
the body chunks in `receive` and the `Importer.add` call (which already serializes per library). Both happen while
the entry is in `uploading` or `importing`, states that `reap`, `claim` and `receive` refuse to touch. File writes
are synchronous 1 MiB writes, as in the current `upload.receive`. The registry is the source of truth. Directories
are created by `prepare` and removed by `consume`, by `reap`, or by `receive`'s failure path when the entry was
dropped. Crash leftovers are removed by the startup `sweep`. The design assumes a single process; this is
documented.

Path safety: lookups use the registry dict keyed by the exact `file_id` string, so on-disk paths come only from the
registry entry, never from the request. `ID_RE.fullmatch` is checked first as defence in depth. The filename is
`safe_filename`-ed at prepare time, and Calibre keeps reading it (its stem is the fallback title).

### `server.py` changes

```python
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

@mcp.tool(annotations=WRITE)
def prepare_upload(filename: str, ctx: Context) -> dict:
    """Start adding a book: returns an upload_url to PUT the file's bytes to (e.g. curl --fail-with-body -T
    ./book.epub '<upload_url>') and a file_id for add_book. The filename's extension picks the format.
    The upload expires after a period of inactivity; one file per call."""

@mcp.tool(annotations=WRITE)
async def add_book(file_id: str, ctx: Context, library: str | None = None,
                   title: str | None = None, authors: list[str] | None = None, tags: list[str] | None = None,
                   series: str | None = None, series_index: float | None = None,
                   languages: list[str] | None = None, identifiers: dict[str, str] | None = None,
                   duplicates: Literal["refuse", "new_record", "merge"] = "refuse") -> dict:
    """Add a book uploaded via prepare_upload to a library (default: the first). Metadata fields override the
    ebook's own. duplicates: refuse (default; returns status "duplicate" with the existing books and keeps the
    upload so you can retry), new_record (add another record), or merge (add this format to the matching book).
    On any error the upload is kept until it expires; fix the arguments and call add_book again."""
```

`add_book` steps, in order:
1. If `cat.can_add` is false → `ToolError("calibredb is not installed; adding books needs Calibre")`.
2. `lib = lib_or_error(library)`, then `metadata = tool_metadata(title, authors, tags, series, series_index,
   languages, identifiers)`. That helper lowercases and strips identifier keys, raises `ToolError` when two keys
   collide, rejects a non-finite `series_index`, builds `BookMetadata` and calls `.args()`; `AddError` →
   `ToolError`. Staging is untouched so far.
3. `staged = cat.stager.claim(file_id)`. `StagingError` → `ToolError(str(exc))`.
4. ```python
   try:
       result = await cat.importer.add(lib.root, staged.path, metadata, duplicates)
   except AddError as exc:
       cat.stager.release(file_id); raise ToolError(str(exc)) from None
   except BaseException:
       cat.stager.release(file_id); raise
   ```
5. `added`/`merged`: call `cat.stager.consume(file_id)` first (the library has already changed), then build `books`
   from `result.book_ids` via `lib.book(id) | cat.links(...)` and skip unreadable ids. No books →
   `ToolError("calibredb reported no readable books")`. Otherwise return `{"status", "library", "file_id",
   "books"}`.
6. `duplicate`: `cat.stager.release(file_id)`. `existing` is built as the POST route does today (`lib.search(title,
   limit=10)` per reported title, deduped by id, at most 10, with links). Return `{"status": "duplicate",
   "library", "file_id", "message": "already in the library: <titles>; the upload is kept — call add_book again with
   duplicates=\"new_record\" or \"merge\" if the user wants that", "existing", "expires_at"}`.

`prepare_upload`: if `can_add` is false → `ToolError`. `StagingError` → `ToolError`. It returns the fields in
acceptance 2, with `upload_url = f"{cat.base_url(ctx.headers)}/uploads/{file_id}"`.

Route: `@mcp.custom_route("/uploads/{file_id}", methods=["PUT"])`. It runs `same_origin(request, cfg.allowed_hosts)`
(403), then `can_add` (503), then `await cat.stager.receive(file_id, request, cat.max_upload)`. `StagingError` →
`JSONResponse({"error"}, status_code=exc.status)`. Success → 200 `{"file_id", "filename", "size", "sha256",
"expires_at"}`.

Lifespan: after `streamable_http_app(...)`, `build_app` wraps `app.router.lifespan_context` with an
`asynccontextmanager`. The wrapper enters the original (the session manager), starts `asyncio.create_task(reaper())`,
where `reaper()` loops `await asyncio.sleep(min(60, cfg.upload_ttl)); cat.stager.reap()` with any exception logged,
and cancels and awaits the task on exit. At startup `build_app` calls `cat.stager.sweep()`, which replaces
`upload.sweep`.

`Catalog`: drop `self.upload_root` usage except as the `Stager` root (`cfg.cache_dir / "uploads"`). Add
`self.stager = Stager(cfg.cache_dir / "uploads", ttl=cfg.upload_ttl, max_bytes=self.max_upload,
max_entries=cfg.max_staged_uploads)`. Keep `self.max_upload`.

`INSTRUCTIONS`: "Tools for Calibre ebook libraries…" (read tools unchanged), and then: "To add a book (when
list_libraries reports add_books): call prepare_upload(filename), upload the file's bytes with the returned method to
upload_url (`curl --fail-with-body -T ./book.epub '<upload_url>'`), then call add_book(file_id, …) with optional
metadata overrides. A duplicate or an error keeps the upload until it expires, so retry add_book without uploading
again; ask the user before using duplicates=new_record or merge."

The module docstring HTTP list: replace the `POST /<library>/books` line with `PUT /uploads/<file_id>   bytes for an
upload started with MCP prepare_upload`.

### Errors and conventions

| Where | Condition | Result |
| --- | --- | --- |
| `prepare_upload` | bad filename / calibredb missing / cap reached | `ToolError`, nothing staged |
| PUT | foreign Origin / calibredb missing / bad or unknown id / bad Content-Length or empty / too big / busy | 403 / 503 / 404 / 400 / 413 / 409, `{"error"}` |
| `add_book` | calibredb missing, bad metadata, bad library, unknown/pending/busy id | `ToolError`, staging unchanged |
| `add_book` | `AddError` from the importer | `ToolError(str(exc))`, file kept, expiry refreshed |
| `add_book` | duplicate | normal result `status: "duplicate"`, file kept, expiry refreshed |
| `add_book` | added / merged | normal result, file consumed |
| `add_book` | ids reported but none readable | `ToolError`, file consumed |

Times: `expires_at` is ISO-8601 UTC with second precision and a `Z` suffix, and `expires_in` is an integer number
of seconds. Logging: `log.info` for staged (`file_id[:6]`, filename), received (size) and consumed; `log.debug` for
reaped counts. Bytes and full ids are never logged.

## Tasks

There are two cohesive tasks, split at the module boundary: a self-contained staging layer, and the MCP/HTTP
integration that consumes it and removes the old endpoint. T2 needs T1's interface, so they run in separate waves.

### Wave 1

**T1: staging layer**
- Goal: implement `staging.py` exactly as specified (`StagingError`, `safe_filename` moved verbatim from
  `upload.py` but raising `StagingError`, `Staged`, `Stager` with its state machine, `receive` with Content-Length
  and streaming caps, sha256 and atomic replace, `claim`/`release`/`consume`/`reap`/`sweep`/`expires_at`/
  `expires_in`, and the injectable `clock`/`wall`). Unit tests cover every transition, race and expiry rule, and
  carry over the `safe_filename` cases from `tests/test_upload.py`. `upload.py` is not touched; for one wave the
  two copies of `safe_filename` coexist.
- Owned files: `src/calibre_mcp/staging.py` (new), `tests/test_staging.py` (new).
- Consumes: `starlette.requests.Request` (and Starlette `TestClient` in tests).
- Produces: the `staging.py` interface above.
- Dependencies: none.
- Unit check: `nix develop -c python -m unittest discover -s tests -v`.

### Wave 2

**T2: MCP tools, upload route, removal of the POST endpoint, config and docs**
- Goal: wire the staged flow into the server. This covers the `prepare_upload` and `add_book` tools, `PUT
  /uploads/{file_id}`, the reaper lifespan, `Stager.sweep` at startup, `list_libraries` (no `add_url`),
  `INSTRUCTIONS`, `GET /` and the module docstring. It deletes the POST route, `upload.py` and
  `tests/test_upload.py`, adds config/env/flags/check output, drops `python-multipart` from `pyproject.toml` and
  `flake.nix`, and rewrites the README and SKILL.md for the staged flow. The existing POST-based tests in
  `tests/test_calibre_mcp.py` (`AddBookTest`) are rewritten onto prepare → PUT → `add_book` through the real MCP
  client over the ASGI transport with the fake calibredb, keeping their coverage (metadata args, duplicates, merge,
  importer errors and stderr diagnostics, timeouts, unreadable ids, unavailable calibredb, the origin guard, the
  startup sweep, check output).
- Owned files: `src/calibre_mcp/server.py`, `src/calibre_mcp/config.py`, `src/calibre_mcp/__main__.py`,
  `src/calibre_mcp/upload.py` (delete), `tests/test_upload.py` (delete), `tests/test_calibre_mcp.py`,
  `tests/test_importer.py` (config tests only), `pyproject.toml`, `flake.nix`, `README.md`, `SKILL.md`.
- Consumes: the T1 `staging.py` interface; `Importer.add`, `BookMetadata`, `AddError`, `AddResult` (unchanged).
- Produces: the MCP tools `prepare_upload` and `add_book`, the HTTP route `PUT /uploads/{file_id}`, and the config
  keys `upload_ttl` and `max_staged_uploads`.
- Dependencies: T1.
- Unit check: `nix develop -c python -m unittest discover -s tests -v`.

## Test and verification

Required for every task, push and merge: `nix develop -c python -m unittest discover -s tests -v`. The repository
has no lint, type-check or build gate, and none is added. After T2, an optional extra check before the merge is
`nix build`, which runs the same suite via `unittestCheckHook` and verifies that the flake deps change still
builds. The Dockerfile build also runs the suite, but it isn't required.

New and rewritten unit tests:

- `tests/test_staging.py` (T1), with a fake clock/wall and a minimal Starlette app whose PUT route calls
  `Stager.receive`:
  - `safe_filename` cases (carried over). `prepare` sanitises the name, rejects bad names (400), enforces the cap
    (429), and creates the directory. ids match `ID_RE` and are unique.
  - receive: happy path (size, sha256, file content, no `.part`); a re-PUT replaces the bytes; empty → 400; an
    invalid `Content-Length` → 400; declared and streamed over-cap → 413 with the earlier bytes kept, no `.part`
    and the state restored; unknown or malformed id → 404; PUT while importing → 409; the `max_bytes` override is
    honoured; a body stream that raises mid-way restores `pending`/`ready`.
  - claim/release/consume transitions and their exact messages and statuses; a double claim → the second gets 409.
  - reap removes expired pending and ready entries plus their directories, and skips uploading and importing ones.
    An import that runs past the deadline is not reaped. `release` counts as activity, so the entry comes back
    `ready` with a fresh deadline. Sliding expiry is refreshed by PUT and by release. `expires_at`/`expires_in` come
    from the fake clocks.
  - Races: during a slow `receive` (a body generator gated by an `asyncio.Event`), `claim` → 409 and `reap` is a
    no-op. Advance the clock past expiry during the receive and let it fail → the entry and its directory are gone.
  - sweep removes `.staged-*` and `.upload-*` directories only.
- `tests/test_calibre_mcp.py` (T2), using `mcp.Client` and `httpx2.ASGITransport` as
  `test_discovery_and_search_after_upload` does today, with a small helper `stage(client, http, name, body)`:
  - The tool set and annotations. prepare → PUT → `add_book` with every metadata field reaching the fake
    calibredb's `last-args.json`; the EPUB download returns the bytes; the staged directory is gone; the id is
    then unknown.
  - Duplicate → `status: "duplicate"` with `existing == [4]`; the retry with `new_record` works without a re-PUT;
    merge → `merged`; merge of an existing format → duplicate.
  - Bad metadata (empty title, `series_index` without series, colliding identifier keys), an unknown library, and a
    patched `AddError` (503/502, with the stderr-tail and timeout diagnostics via a patched `subprocess.run`) →
    `ToolError` with the exact message, the file is kept, and the next `add_book` succeeds.
  - Unreadable ids → `ToolError`, consumed; mixed ids → only the readable books.
  - Pending, busy and unknown ids. PUT 403 (foreign Origin), 200 with `X-Forwarded-Host` origin, `allowed_hosts`
    origin, 404, 413 (lowered `cat.max_upload`), 409, 503 (calibredb missing), and `POST /main/books` non-2xx.
  - Two concurrent `add_book` calls on one id with a slow patched importer → one call to `Importer.add`, and the
    other gets the busy `ToolError`.
  - The startup sweep of `.staged-*` and `.upload-*`. `list_libraries` without `add_url`. The `GET /` fields. The
    `check` output with the new fields.
- `tests/test_importer.py` (T2): defaults, TOML, env and validation for `upload_ttl` and `max_staged_uploads`.

## E2E journey

**Sandbox E2E execution is exempt for this repository** (AGENTS.md: "Sandbox end-to-end testing is not needed for
this repository, as explicitly decided by the user"). The journey below is the user-level behaviour the design must
deliver. It is not a sandbox requirement and adds none. The in-process unit tests above exercise the same steps
with a fake calibredb, and the existing unit command stays mandatory.

1. The agent calls `list_libraries` and sees `add_books: true` (and no `add_url`).
2. The agent calls `prepare_upload(filename="On Food and Cooking.epub")` and gets `file_id`, `upload_url`,
   `method: "PUT"`, `expires_at`, `expires_in`, `max_bytes` and a `curl` hint.
3. The shell runs `curl --fail-with-body -T "$BOOK" '<upload_url>'` → 200 `{"size", "sha256", …}`.
4. The agent calls `add_book(file_id, title=…, authors=[…], tags=[…])` → `status: "added"`, and `books[0]` has its
   id, metadata and `downloads`/`epub_url` links. A `GET` of the EPUB link returns the uploaded bytes.
5. The agent prepares and PUTs the same book again and calls `add_book` → `status: "duplicate"` with `existing`. The
   agent asks the user, then calls `add_book(file_id, duplicates="new_record")` without re-uploading → `added`.
6. The agent calls `add_book` with the consumed `file_id` → error "unknown or expired file_id; call prepare_upload
   again".
7. A prepared upload that is never imported disappears after `upload_ttl` of inactivity, and a PUT to its URL → 404.
8. The old `curl -F file=@book.epub <base>/main/books` no longer adds a book (non-2xx).
