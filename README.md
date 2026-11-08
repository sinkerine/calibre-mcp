# calibre-mcp

An MCP server over one or more [Calibre](https://calibre-ebook.com) libraries, plus plain HTTP links to
every book file. Reads use `metadata.db` read-only; adds go through Calibre’s `calibredb`.

- **MCP tools** (streamable HTTP at `/mcp`, stateless):
  `list_libraries`, `search_books`, `get_book`, `browse_library` (authors / tags / series / publishers /
  languages), plus `prepare_upload` and `add_book` for imports. Every book comes with direct download URLs per stored format, an `epub_url` and a `cover_url`.
- **Direct links**, usable by anything that speaks HTTP:

  | URL | |
  | --- | --- |
  | `/<library>/books/<id>` | metadata (JSON) |
  | `/<library>/books/<id>/<format>` | a stored file: `epub`, `pdf`, `mobi`, `azw3`, `cbz`, … |
  | `/<library>/books/<id>/epub` | the EPUB; converted with Calibre's `ebook-convert` (cached) when the book has none |
  | `/<library>/books/<id>/cover.jpg` | cover |
  | `/health`, `/` | health, index |

There is **no authentication**: run it behind a proxy that limits who can reach it (e.g. a LAN-only
Traefik router). Links are built from the request's `Host` / `X-Forwarded-*` headers, so clients that reach
the service by different names (a proxy hostname, a Docker service name) each get links that work for them;
set `public_url` to force one base.

## Configuration

A TOML file (`--config` or `$CALIBRE_MCP_CONFIG`), then environment variables, then flags:

```toml
host = "0.0.0.0"
port = 8000
public_url = ""              # base for returned links; empty = from each request
cache_dir = "/cache"         # converted EPUBs
convert = true               # offer EPUBs converted from MOBI/AZW3/PDF/… (needs ebook-convert)
ebook_convert = "ebook-convert"
convert_timeout = 900
allowed_hosts = []           # Host headers /mcp accepts; empty = any
calibredb = "calibredb"       # executable for adding books
add_timeout = 300            # seconds
max_upload_mb = 1024         # maximum PUT body size in MiB
upload_ttl = 3600           # inactivity timeout, seconds (minimum 60)
max_staged_uploads = 16     # maximum live staged files (minimum 1)

[[libraries]]                # the first one is the default
name = "main"
path = "/books"              # directory that holds metadata.db
```

| Variable | |
| --- | --- |
| `CALIBRE_MCP_LIBRARIES` | `main=/books,other=/other` (replaces `[[libraries]]`) |
| `CALIBRE_MCP_HOST`, `CALIBRE_MCP_PORT` | listen address |
| `CALIBRE_MCP_PUBLIC_URL` | fixed base URL for links |
| `CALIBRE_MCP_CACHE_DIR` | conversion cache |
| `CALIBRE_MCP_CONVERT` | `0` disables conversion |
| `CALIBRE_MCP_EBOOK_CONVERT` | path to `ebook-convert` |
| `CALIBRE_MCP_ALLOWED_HOSTS` | comma-separated |
| `CALIBRE_MCP_CALIBREDB` | path to `calibredb` (`--calibredb PATH`) |
| `CALIBRE_MCP_MAX_UPLOAD_MB` | maximum PUT body size in MiB (`--max-upload-mb N`) |
| `CALIBRE_MCP_UPLOAD_TTL` | inactivity timeout in seconds (`--upload-ttl N`) |
| `CALIBRE_MCP_MAX_STAGED_UPLOADS` | maximum live staged files (`--max-staged-uploads N`) |

```sh
calibre-mcp check -l main=/srv/calibre          # print libraries and recent books
calibre-mcp -l main=/srv/calibre --port 8000     # serve
```

## Adding books

Add books in three steps using an MCP client and a shell:

1. Call MCP `prepare_upload(filename="book.epub")`. It returns `file_id`, `upload_url`, `method: "PUT"`,
   sanitized `filename`, `max_bytes`, `expires_at`, `expires_in` and a curl hint.
2. Upload the file bytes from the machine that holds the file:

   ```sh
   curl --fail-with-body -T ./book.epub '<upload_url>'
   ```

3. Call MCP `add_book(file_id="<file_id>", title="A new book", authors=["A", "B"], tags=["fiction"])`.
   Omit `library` to use the first configured library. The result has `status: "added"` or `"merged"`,
   `library`, `file_id` and `books` with metadata and download links. Success consumes the staged upload.

The four discovery tools remain read-only; the two new tools change server state. `list_libraries` reports
`add_books`; `calibre-mcp check` also reports whether adding is available. Imports require `calibredb`
on PATH (or configured with `calibredb`), a writable cache and a **read-write** library mount.
Bytes travel over HTTP, never through MCP. No server paths or import mounts are needed.

Metadata parameters are flat: `title` (non-empty string), `authors`, `tags`, `languages` (lists of strings),
`series` (string), `series_index` (finite number requiring series), and `identifiers` (a dictionary such as
`{"isbn": "9780684800011"}`). Identifier keys are case-insensitive and must be unique; keys allow letters,
digits, `_` and `-`. Values must be non-empty and cannot contain commas. Unspecified metadata comes from
the ebook. The filename needs a stem and extension; its extension selects the format.

`duplicates="refuse"` is the default. A duplicate is a normal MCP result with `status: "duplicate"`,
`message`, matching books in `existing`, and `expires_at`. The upload stays available: retry `add_book`
with the same `file_id` without uploading again. Ask the user before using `duplicates="new_record"`
(to create another record) or `duplicates="merge"` (to add a missing format). Merge keeps existing
formats and reports a duplicate if the format is already present. Calibre may match multiple records.

Invalid metadata, unknown libraries and import failures are MCP tool errors and keep the upload for a
corrected retry. An import that changed the library but returned no readable books consumes the upload.
`add_timeout` sets the calibredb timeout in seconds (default 300).

PUT returns HTTP **200** with `file_id`, `filename`, `size`, `sha256` and `expires_at`. A second PUT replaces
the staged bytes. Errors return JSON `error`: **400** for an empty or malformed body, **403** for a foreign
browser Origin, **404** for an unknown or expired id, **409** while uploading/importing, **413** for a body
above `max_upload_mb` (default 1024 MiB), and **503** when calibredb is unavailable. The browser Origin
must match the request host or an entry in `allowed_hosts`.

Staged files live under `<cache_dir>/uploads`. `upload_ttl` defaults to 3600 seconds of inactivity,
refreshed by a successful PUT or a failed import. `max_staged_uploads` defaults to 16; worst-case staging
disk use is that cap times `max_upload_mb`. Expired uploads are removed lazily and by a background reaper.
The registry lives in one process on one event loop; run a single uvicorn worker. Restarting drops all
staged uploads, and startup removes crash leftovers. Keep upload URLs and file ids private: they are
capabilities. The service has no authentication; restrict access at your proxy. Do not add while the
Calibre desktop app has the same library open.

## Docker

The image includes Calibre for EPUB conversion and adding books (`--build-arg WITH_CALIBRE=0` leaves it out). Mount the
library read-write and run as a user that can read and write it:

```yaml
services:
  calibre-mcp:
    build: .
    image: calibre-mcp:local
    restart: unless-stopped
    user: "1000:1000"
    environment:
      CALIBRE_MCP_LIBRARIES: main=/libraries/main
    volumes:
      - /srv/calibre:/libraries/main:rw
      - ./persist/cache:/cache
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.calibre-mcp.rule=Host(`calibre-mcp.example.lan`)"
      - "traefik.http.routers.calibre-mcp.middlewares=lan-only@file"
      - "traefik.http.services.calibre-mcp.loadbalancer.server.port=8000"
```

The proxy body-size limit (for example Traefik buffering) must allow the intended upload size.

Connect an MCP client: `claude mcp add --transport http calibre https://calibre-mcp.example.lan/mcp`.

## Development

```sh
nix develop
python -m unittest discover -s tests     # synthetic library; HTTP routes and MCP tools
```
