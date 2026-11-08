# calibre-mcp

An MCP server over one or more [Calibre](https://calibre-ebook.com) libraries, plus plain HTTP links to
every book file. Reads use `metadata.db` read-only; adds go through Calibre’s `calibredb`.

- **MCP tools** (streamable HTTP at `/mcp`, stateless):
  `list_libraries`, `search_books`, `get_book`, `browse_library` (authors / tags / series / publishers /
  languages). Every book comes with direct download URLs per stored format, an `epub_url` and a `cover_url`.
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
max_upload_mb = 1024         # maximum request size in MiB, including multipart overhead

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
| `CALIBRE_MCP_MAX_UPLOAD_MB` | maximum request size in MiB (`--max-upload-mb N`) |

```sh
calibre-mcp check -l main=/srv/calibre          # print libraries and recent books
calibre-mcp -l main=/srv/calibre --port 8000     # serve
```

## Adding books

Upload to `POST /<library>/books`. This requires `calibredb` on PATH (or configured with `calibredb`)
and a **read-write** library mount. `list_libraries` advertises `add_books` and each library's `add_url`;
`calibre-mcp check` also reports whether adding is available. The four MCP tools remain read-only.

```sh
curl -i -F file=@book.epub -F title='A new book' -F 'authors=A & B' \
  -F tags=fiction,favourites http://localhost:8000/main/books
curl -i --data-binary @book.epub \
  'http://localhost:8000/main/books?filename=book.epub&title=A%20new%20book'
```

Multipart uses a `file` part; an optional `filename` field overrides its filename. Raw bodies require
`?filename=` or a `Content-Disposition: attachment; filename="book.epub"` header. Filenames must include
an extension. Metadata fields are `title`, `authors` (split on `&`, or repeated), `tags` and `languages`
(comma-separated, or repeated), `series`, `series_index` (number, requires series), and repeated
`identifier=type:value` (for example `identifier=isbn:9780684800011`). Use form fields for multipart
and query parameters for raw bodies.

`duplicates=refuse` is the default: duplicates return **409** with matching books in `existing`.
`duplicates=new_record` creates another record. `duplicates=merge` merges into an existing book and
returns **200**, or **409** if that format already exists. A new book returns **201** with metadata,
download links and a `Location` header. Invalid input returns **400**, unknown libraries **404**,
oversized requests **413**, unavailable calibredb or a locked library **503**, and failed or timed-out
imports **502**. Error bodies contain `error`; duplicate responses contain `status`, `message` and `existing`.

`max_upload_mb` defaults to 1024 MiB and caps the entire body, including multipart overhead.
Temporary uploads live under `<cache_dir>/uploads` and are removed after requests; crash leftovers are
swept on startup. `add_timeout` sets the calibredb timeout in seconds (default 300).

There is no auth: restrict POST at your proxy if needed. Browser requests with a foreign `Origin` are
refused with **403**; the origin host must match the request host or an entry in `allowed_hosts`.
Do not add while the Calibre desktop app has the same library open.

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
