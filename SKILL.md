---
name: calibre-add-book
description: Add ebooks through calibre-mcp using prepare_upload, HTTP PUT, and add_book, or update existing book metadata with update_book.
---

# Add a book to Calibre

Use the MCP server on sachi (`https://calibre-mcp.sachi.m.mado.moe/mcp`) and a shell on a machine
that can reach it and read the ebook. Call `list_libraries` to check `add_books` and library names;
omitting `library` selects the default.

1. Call MCP `prepare_upload(filename="On Food and Cooking.epub")`. Save its `file_id` and `upload_url`.
   The response also contains `method: "PUT"`, sanitized `filename`, `max_bytes`, `expires_at`,
   `expires_in`, and a curl hint. The filename needs a stem and extension; the extension selects format.
2. Send the local file's bytes to the returned URL:

   ```sh
   BOOK='/pool/sub/download/aria2/Harold McGee - On Food and Cooking (2004).epub'
   curl --silent --show-error --fail-with-body -T "$BOOK" '<upload_url>'
   ```

   The path is local to the machine running curl. HTTP 200 confirms staging with `file_id`, `filename`,
   `size`, `sha256`, and `expires_at`. A repeated PUT replaces the staged bytes.
3. Call MCP `add_book(file_id="<file_id>")` to use the ebook's embedded metadata, or pass flat overrides:

   ```json
   {
     "file_id": "<file_id>",
     "library": "main",
     "title": "On Food and Cooking: The Science and Lore of the Kitchen",
     "authors": ["Harold McGee"],
     "tags": ["cooking", "food science", "reference"],
     "languages": ["eng"],
     "identifiers": {"isbn": "9780684800011"}
   }
   ```

The result contains `status: "added"` or `"merged"`, `library`, `file_id`, and `books` with IDs,
metadata and download links. Success consumes the staged upload; the local source file is kept.
Bytes never go through MCP, and no server path mapping is needed.

`add_book` and `update_book` accept the same metadata fields. `authors`, `tags`, and `languages` are lists of strings.
An author cannot contain `&`; tags and language codes cannot contain commas. `title` must be non-empty.
`series` is a string; `series_index` is a finite number requiring `series`. `identifiers` is a dictionary:
keys are unique without regard to case and allow letters, digits, `_`, and `-`; values must be non-empty
and cannot contain commas. `publisher` is a string; `description` accepts text or HTML.

To edit an existing book, call `update_book(book_id=42, publisher="Example Press", description="New text",
series="Example Series", series_index=2.5)`. Omit `library` to use the default. The result is the full
book metadata and download links. Omitted/null fields stay unchanged; supplied lists and identifiers
replace existing values. Empty lists/dictionaries clear fields (empty authors become Unknown in Calibre);
empty series/publisher/description clears those fields. Title must be non-empty. A series number can
be updated alone when the book already belongs to a series. At least one field is required.
`list_libraries` reports `update_books` availability. Updates change library metadata, not embedded
metadata in ebook files.

The default `duplicates="refuse"` returns a normal MCP result with `status: "duplicate"`, `message`,
matching books in `existing`, and `expires_at`. The file stays staged. Ask the user before retrying
`add_book` with the same `file_id` and `duplicates="new_record"` (another record) or `duplicates="merge"`
(add a missing format). No new PUT is needed. Merge does not replace an existing format and returns a
duplicate when that format already exists. It may match multiple records; if no record matches it may
add a new one. Supply title and author overrides when needed for matching.

Tool errors keep the upload until expiry: correct metadata or the library and retry `add_book`. An
unknown or expired id requires a new `prepare_upload` and PUT. An id with no bytes requires PUT first;
a busy id requires waiting. If Calibre changed the library but returned no readable books, the upload
is consumed; inspect the library before adding again. Publisher and description are applied after
import/merge. If that metadata step fails, the error reports the imported book IDs and consumes the
upload; retry the metadata with `update_book`.

PUT errors have JSON `error`: 400 empty/malformed body, 403 foreign browser Origin, 404 unknown/expired
id, 409 busy upload/import, 413 size limit, 503 unavailable calibredb. Imports report locked libraries,
Calibre failures and timeouts as MCP tool errors. Do not add while the Calibre desktop app has the
same library open.

The default limit is 1024 MiB per file, 16 live staged uploads, and 3600 seconds of inactivity. A successful
PUT or failed import refreshes expiry. Abandoned files expire; a restart drops staged uploads. The server
uses a single process and event loop with a local disk cache. It needs a writable library and cache and
an executable `calibredb`. Upload URLs and file ids are capabilities: keep them private. The service has
no authentication and is reached through the existing LAN-only proxy.
