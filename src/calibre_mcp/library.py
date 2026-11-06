"""Read-only access to a Calibre library (metadata.db plus the book folders next to it).

The database is opened read-only per query, so Calibre / calibre-web can keep
writing to it; a query that meets a write in progress is retried briefly.
"""

from __future__ import annotations

import html
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

# File formats whose conversion to EPUB is worth offering, best source first.
CONVERT_FROM = ["AZW3", "MOBI", "AZW", "KF8", "DOCX", "FB2", "HTMLZ", "ODT", "RTF", "LIT", "PDB", "TXT", "PDF",
                "CBZ", "CBR", "CB7", "DJVU"]
SORTS = {
    "added": "b.timestamp DESC",
    "title": "b.sort COLLATE NOCASE ASC",
    "author": "b.author_sort COLLATE NOCASE ASC, b.sort COLLATE NOCASE ASC",
    "published": "b.pubdate DESC",
    "modified": "b.last_modified DESC",
}
KINDS = {
    "authors": ("authors", "books_authors_link", "author", "name"),
    "tags": ("tags", "books_tags_link", "tag", "name"),
    "series": ("series", "books_series_link", "series", "name"),
    "publishers": ("publishers", "books_publishers_link", "publisher", "name"),
    "languages": ("languages", "books_languages_link", "lang_code", "lang_code"),
}
_TAGS = re.compile(r"<[^>]+>")


def plain(text: str | None) -> str:
    """Calibre comments are HTML; give a readable plain-text version."""
    if not text:
        return ""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", text)
    text = html.unescape(_TAGS.sub("", text))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Library:
    def __init__(self, name: str, root: Path) -> None:
        self.name = name
        self.root = Path(root).resolve()
        self.db_path = self.root / "metadata.db"

    # -- plumbing ---------------------------------------------------------------

    def _query(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        uri = f"file:{quote(str(self.db_path))}?mode=ro"
        for attempt in range(5):
            try:
                con = sqlite3.connect(uri, uri=True, timeout=5)
                try:
                    con.row_factory = sqlite3.Row
                    return con.execute(sql, args).fetchall()
                finally:
                    con.close()
            except sqlite3.OperationalError as exc:  # locked / hot journal while Calibre writes
                if attempt == 4 or not re.search(r"locked|busy|readonly|disk I/O", str(exc), re.I):
                    raise
                time.sleep(0.2 * (attempt + 1))
        raise AssertionError("unreachable")

    def _many(self, sql: str, ids: list[int]) -> dict[int, list]:
        """Run `sql` (with one `{ids}` placeholder, selecting book + value) for many books."""
        out: dict[int, list] = {i: [] for i in ids}
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            for r in self._query(sql.format(ids=",".join("?" * len(chunk))), chunk):
                out[r[0]].append(r[1] if len(r) == 2 else tuple(r[1:]))
        return out

    # -- books ------------------------------------------------------------------

    def count(self) -> int:
        return self._query("SELECT count(*) FROM books")[0][0]

    def formats(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self._query("SELECT format, count(*) FROM data GROUP BY format ORDER BY 2 DESC")}

    def search(self, query: str | None = None, *, author: str | None = None, tag: str | None = None,
               series: str | None = None, language: str | None = None, format: str | None = None,
               sort: str = "relevance", limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
        where, args, order_args = [], [], []
        for term in (query or "").split():
            if term.isdigit():
                where.append("(b.id = ? OR b.title LIKE ? ESCAPE '\\')")
                args += [int(term), _like(term)]
                continue
            like = _like(term)
            where.append(
                "(b.title LIKE ? ESCAPE '\\' OR b.sort LIKE ? ESCAPE '\\'"
                " OR EXISTS (SELECT 1 FROM books_authors_link l JOIN authors a ON a.id = l.author"
                "            WHERE l.book = b.id AND a.name LIKE ? ESCAPE '\\')"
                " OR EXISTS (SELECT 1 FROM books_tags_link l JOIN tags t ON t.id = l.tag"
                "            WHERE l.book = b.id AND t.name LIKE ? ESCAPE '\\')"
                " OR EXISTS (SELECT 1 FROM books_series_link l JOIN series s ON s.id = l.series"
                "            WHERE l.book = b.id AND s.name LIKE ? ESCAPE '\\'))")
            args += [like] * 5
        if author:
            where.append("EXISTS (SELECT 1 FROM books_authors_link l JOIN authors a ON a.id = l.author "
                         "WHERE l.book = b.id AND a.name LIKE ? ESCAPE '\\')")
            args.append(_like(author))
        if tag:
            where.append("EXISTS (SELECT 1 FROM books_tags_link l JOIN tags t ON t.id = l.tag "
                         "WHERE l.book = b.id AND t.name LIKE ? ESCAPE '\\')")
            args.append(_like(tag))
        if series:
            where.append("EXISTS (SELECT 1 FROM books_series_link l JOIN series s ON s.id = l.series "
                         "WHERE l.book = b.id AND s.name LIKE ? ESCAPE '\\')")
            args.append(_like(series))
        if language:
            where.append("EXISTS (SELECT 1 FROM books_languages_link l JOIN languages g ON g.id = l.lang_code "
                         "WHERE l.book = b.id AND g.lang_code = ?)")
            args.append(language.lower())
        if format:
            where.append("EXISTS (SELECT 1 FROM data d WHERE d.book = b.id AND d.format = ?)")
            args.append(format.upper())
        cond = " AND ".join(where) or "1"
        if sort in SORTS:
            order = SORTS[sort]
        elif query and query.strip():  # relevance: title hits first, then newest
            order = "(b.title LIKE ? ESCAPE '\\') DESC, b.timestamp DESC"
            order_args = [_like(query.strip())]
        else:
            order = SORTS["added"]
        total = self._query(f"SELECT count(*) FROM books b WHERE {cond}", args)[0][0]
        rows = self._query(f"SELECT b.id FROM books b WHERE {cond} ORDER BY {order} LIMIT ? OFFSET ?",
                           [*args, *order_args, max(1, min(limit, 200)), max(0, offset)])
        return total, self.summaries([r[0] for r in rows])

    def summaries(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        books = {r["id"]: r for r in self._query(
            f"SELECT id, title, timestamp, pubdate, series_index, has_cover FROM books WHERE id IN ({q})", ids)}
        authors = self._many("SELECT l.book, a.name FROM books_authors_link l JOIN authors a ON a.id = l.author "
                             "WHERE l.book IN ({ids}) ORDER BY l.id", ids)
        tags = self._many("SELECT l.book, t.name FROM books_tags_link l JOIN tags t ON t.id = l.tag "
                          "WHERE l.book IN ({ids}) ORDER BY t.name", ids)
        series = self._many("SELECT l.book, s.name FROM books_series_link l JOIN series s ON s.id = l.series "
                            "WHERE l.book IN ({ids})", ids)
        langs = self._many("SELECT l.book, g.lang_code FROM books_languages_link l JOIN languages g "
                           "ON g.id = l.lang_code WHERE l.book IN ({ids}) ORDER BY l.item_order", ids)
        fmts = self._many("SELECT book, format FROM data WHERE book IN ({ids}) ORDER BY format", ids)
        out = []
        for i in ids:
            b = books.get(i)
            if b is None:
                continue
            item = {"id": i, "title": b["title"], "authors": authors[i], "formats": fmts[i],
                    "has_cover": bool(b["has_cover"]),
                    "languages": langs[i], "tags": tags[i], "added": (b["timestamp"] or "")[:10],
                    "published": (b["pubdate"] or "")[:10] if not (b["pubdate"] or "").startswith("0101") else ""}
            if series[i]:
                item["series"] = series[i][0]
                item["series_index"] = b["series_index"]
            out.append(item)
        return out

    def book(self, book_id: int) -> dict | None:
        found = self.summaries([int(book_id)])
        if not found:
            return None
        item = found[0]
        b = self._query("SELECT uuid, last_modified FROM books WHERE id = ?", (book_id,))[0]
        pubs = self._query("SELECT p.name FROM books_publishers_link l JOIN publishers p ON p.id = l.publisher "
                           "WHERE l.book = ?", (book_id,))
        rating = self._query("SELECT r.rating FROM books_ratings_link l JOIN ratings r ON r.id = l.rating "
                             "WHERE l.book = ?", (book_id,))
        comments = self._query("SELECT text FROM comments WHERE book = ?", (book_id,))
        idents = self._query("SELECT type, val FROM identifiers WHERE book = ?", (book_id,))
        files = self._query("SELECT format, uncompressed_size, name FROM data WHERE book = ? ORDER BY format",
                            (book_id,))
        item.update(
            uuid=b["uuid"], modified=(b["last_modified"] or "")[:19],
            publisher=pubs[0][0] if pubs else None, rating=(rating[0][0] / 2) if rating and rating[0][0] else None,
            identifiers={r["type"]: r["val"] for r in idents},
            description=plain(comments[0]["text"]) if comments else "",
            files=[{"format": r["format"], "bytes": r["uncompressed_size"]} for r in files],
        )
        return item

    # -- files ------------------------------------------------------------------

    def _inside(self, path: Path) -> Path | None:
        path = path.resolve()
        return path if path.is_relative_to(self.root) and path.is_file() else None

    def file(self, book_id: int, fmt: str) -> Path | None:
        rows = self._query("SELECT b.path, d.name FROM books b JOIN data d ON d.book = b.id "
                           "WHERE b.id = ? AND d.format = ?", (book_id, fmt.upper()))
        if not rows:
            return None
        return self._inside(self.root / rows[0]["path"] / f"{rows[0]['name']}.{fmt.lower()}")

    def cover(self, book_id: int) -> Path | None:
        rows = self._query("SELECT path, has_cover FROM books WHERE id = ?", (book_id,))
        if not rows or not rows[0]["has_cover"]:
            return None
        return self._inside(self.root / rows[0]["path"] / "cover.jpg")

    def epub_source(self, book_id: int, formats: list[str]) -> tuple[str, str] | None:
        """("original", "EPUB") when an EPUB exists, ("convert", <fmt>) when one can be made, else None."""
        for fmt in ("EPUB", "KEPUB", "ORIGINAL_EPUB"):
            if fmt in formats:
                return "original", fmt
        for fmt in CONVERT_FROM:
            if fmt in formats:
                return "convert", fmt
        return None

    def file_name(self, book: dict, ext: str) -> str:
        who = book["authors"][0] if book.get("authors") else ""
        stem = f"{book['title']} - {who}" if who else book["title"]
        stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", stem).strip(" .")[:150] or f"book-{book['id']}"
        return f"{stem}.{ext}"

    # -- browsing -----------------------------------------------------------------

    def browse(self, kind: str, filter: str | None = None, limit: int = 50) -> list[dict]:
        table, link, col, name = KINDS[kind]
        args: list = []
        cond = ""
        if filter:
            cond = f"WHERE x.{name} LIKE ? ESCAPE '\\'"
            args.append(_like(filter))
        rows = self._query(f"SELECT x.{name} AS name, count(l.book) AS books FROM {table} x "
                           f"JOIN {link} l ON l.{col} = x.id {cond} GROUP BY x.id ORDER BY books DESC, name "
                           f"LIMIT ?", [*args, max(1, min(limit, 500))])
        return [{"name": r["name"], "books": r["books"]} for r in rows]
