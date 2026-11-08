"""calibre-mcp command line: `serve` (default), `check`."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .config import LibraryConfig, load_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="calibre-mcp", description="MCP server and direct links for Calibre libraries")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "check"],
                        help="serve (default), or check: print the libraries and a few recent books")
    parser.add_argument("-c", "--config", type=Path, help="TOML config file (default: $CALIBRE_MCP_CONFIG)")
    parser.add_argument("-l", "--library", action="append", metavar="[NAME=]PATH",
                        help="Calibre library directory (repeatable; replaces the configured ones)")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--public-url", help="base URL for returned links (default: from each request)")
    parser.add_argument("--cache-dir", type=Path, help="where converted EPUBs are kept")
    parser.add_argument("--no-convert", action="store_true", help="do not convert books to EPUB")
    parser.add_argument("--calibredb", help="path to calibredb for adding books")
    parser.add_argument("--max-upload-mb", type=int, help="maximum upload size in MiB")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.library:
        cfg.libraries = []
        for spec in args.library:
            name, sep, path = spec.partition("=")
            cfg.libraries.append(LibraryConfig(name, Path(path)) if sep else LibraryConfig(Path(spec).name, Path(spec)))
    for key in ("host", "port", "cache_dir", "calibredb", "max_upload_mb"):
        if getattr(args, key) is not None:
            setattr(cfg, key, getattr(args, key))
    if args.public_url:
        cfg.public_url = args.public_url.rstrip("/")
    if args.no_convert:
        cfg.convert = False
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "check":
        from .server import Catalog

        cfg.validate()
        cat = Catalog(cfg)
        for lib in cat.libraries.values():
            total, recent = lib.search(limit=3)
            print(json.dumps({"library": lib.name, "path": str(lib.root), "books": total, "formats": lib.formats(),
                              "recent": [f"{b['id']}: {b['title']}" for b in recent]}, ensure_ascii=False, indent=2))
        print(f"convert to EPUB: {'yes' if cat.can_convert else 'no'} (cache: {cfg.cache_dir})")
        print(f"add books: {'yes' if cat.can_add else 'no'} "
              f"(calibredb: {cfg.calibredb if cat.can_add else 'missing'}; max upload: {cfg.max_upload_mb} MB)")
        return

    import uvicorn

    from .server import build_app

    uvicorn.run(build_app(cfg), host=cfg.host, port=cfg.port, proxy_headers=True, forwarded_allow_ips="*",
                log_level="info")


if __name__ == "__main__":
    main()
