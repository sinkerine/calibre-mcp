# calibre-mcp

An MCP server over one or more [Calibre](https://calibre-ebook.com) libraries, plus plain HTTP links to
every book file. It reads `metadata.db` read-only, so Calibre or calibre-web can keep managing the library.

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

```sh
calibre-mcp check -l main=/srv/calibre          # print libraries and recent books
calibre-mcp -l main=/srv/calibre --port 8000     # serve
```

## Docker

The image includes Calibre for EPUB conversion (`--build-arg WITH_CALIBRE=0` leaves it out). Mount the
library read-only and run as a user that can read it:

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
      - /srv/calibre:/libraries/main:ro
      - ./persist/cache:/cache
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.calibre-mcp.rule=Host(`calibre-mcp.example.lan`)"
      - "traefik.http.routers.calibre-mcp.middlewares=lan-only@file"
      - "traefik.http.services.calibre-mcp.loadbalancer.server.port=8000"
```

Connect an MCP client: `claude mcp add --transport http calibre https://calibre-mcp.example.lan/mcp`.

## Development

```sh
nix develop
python -m unittest discover -s tests     # synthetic library; HTTP routes and MCP tools
```
