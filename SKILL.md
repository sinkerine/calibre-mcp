---
name: calibre-add-book
description: Upload ebooks to the Calibre library on sachi over HTTP, optionally overriding title, authors, tags, language, series, and identifiers. Use when asked to add or import a book through calibre-mcp.
---

# Add a book to Calibre

Upload a file with `POST /<library>/books`. On sachi, the default library is `main`:

```sh
BASE_URL='https://calibre-mcp.sachi.m.mado.moe'
BOOK='/pool/sub/download/aria2/Harold McGee - On Food and Cooking (2004).epub'
```

Run the commands on a machine that can reach this LAN endpoint and read `$BOOK`. The file path is
local to the machine running `curl`; change it if the ebook is elsewhere. The server receives the
file contents, so no Docker import mount or server-path mapping is required.

## Basic upload

Keep the ebook's embedded metadata:

```sh
curl --silent --show-error --fail-with-body --include \
  --form "file=@${BOOK}" \
  "$BASE_URL/main/books"
```

A successful new import returns HTTP **201** and JSON containing `status`, `library`, and `books`.
Each returned book includes its ID, metadata, and download links. The response also includes a
`Location` header for the book's metadata URL. The source file is kept.

## Override metadata during import

Supply only the fields you want to override; Calibre reads the remaining metadata from the ebook.
Use `--form-string` for metadata so values starting with `@` or `<` remain literal text.

```sh
curl --silent --show-error --fail-with-body --include \
  --form "file=@${BOOK}" \
  --form-string 'title=On Food and Cooking: The Science and Lore of the Kitchen' \
  --form-string 'authors=Harold McGee' \
  --form-string 'tags=cooking,food science,reference' \
  --form-string 'languages=eng' \
  "$BASE_URL/main/books"
```

Supported metadata fields:

| Form field | Value and behavior |
| --- | --- |
| `title` | One non-empty title. Supply once. |
| `authors` | Authors separated by `&`, or repeat the field for each author. An individual author's name cannot contain `&`. |
| `tags` | Tags separated by commas, or repeat the field. Commas always separate tags. |
| `languages` | Language codes such as `eng` or `zho`, separated by commas or supplied as repeated fields. |
| `series` | Series name. Supply once. |
| `series_index` | A finite number such as `2` or `1.5`; requires `series` in the same request. Supply once. |
| `identifier` | A `type:value` pair; repeat for different types, such as `isbn:9780000000002` and `asin:B000EXAMPLE`. Each type may appear once. Types allow letters, digits, `_`, and `-`; values must be non-empty and cannot contain commas. |

Example for a book with multiple authors, a series, and identifiers (replace the sample metadata
with the actual book's values):

```sh
curl --silent --show-error --fail-with-body --include \
  --form 'file=@/path/to/book.epub' \
  --form-string 'title=Example Book' \
  --form-string 'authors=First Author' \
  --form-string 'authors=Second Author' \
  --form-string 'tags=fiction,favourites' \
  --form-string 'languages=eng' \
  --form-string 'series=Example Series' \
  --form-string 'series_index=2' \
  --form-string 'identifier=isbn:9780000000002' \
  --form-string 'identifier=asin:B000EXAMPLE' \
  "$BASE_URL/main/books"
```

Overrides apply to the import. This endpoint is not an editor for metadata on an existing record.
There are no upload fields for publisher, publication date, description, rating, or cover overrides.

## Duplicate handling

The default is `duplicates=refuse`. A duplicate returns HTTP **409** with `status="duplicate"`,
`message`, and matching books in `existing`; it does not create a new record. With
`--fail-with-body`, curl exits non-zero for HTTP errors while still printing the JSON response.
Do not retry with another duplicate mode unless the user wants that behavior.

To create a separate record even when Calibre finds a duplicate, add this form field to the upload:

```sh
--form-string 'duplicates=new_record'
```

To add a missing format to matching existing records, use `duplicates=merge`:

```sh
curl --silent --show-error --fail-with-body --include \
  --form 'file=@/path/to/book.pdf' \
  --form-string 'duplicates=merge' \
  "$BASE_URL/main/books"
```

A merge into existing records returns HTTP **200**. If the format already exists, it returns **409**;
this mode does not replace that format. Calibre may match multiple records. If no duplicate matches,
Calibre can add a new record instead. Use title/author overrides if needed to supply the correct
metadata for matching; do not use merge as a general metadata-update operation.

## Raw upload alternative

For scripts that already have the file body, send it directly. A raw upload needs a filename with
an extension. Metadata goes in URL-encoded query parameters instead of form fields:

```sh
curl --silent --show-error --fail-with-body --include \
  --header 'Content-Type: application/octet-stream' \
  --data-binary "@${BOOK}" \
  "$BASE_URL/main/books?filename=book.epub&title=On%20Food%20and%20Cooking&authors=Harold%20McGee&languages=eng"
```

Prefer multipart for metadata containing spaces, punctuation, or non-ASCII text so curl handles
encoding. Multipart also accepts an optional `filename` form field to override the uploaded filename;
that filename must include a non-empty stem and extension.

## Discovery and troubleshooting

The MCP tools are read-only. `list_libraries` reports `add_books` and each library's `add_url`; use
that URL for uploads. The service index also reports whether adding books is enabled:

```sh
curl --silent --show-error --fail-with-body "$BASE_URL/"
curl --silent --show-error --fail-with-body "$BASE_URL/health"
```

| HTTP status | Meaning |
| --- | --- |
| `200` | Existing records received a merged format. |
| `201` | A new record was added. |
| `400` | Invalid metadata, missing filename/extension, or an empty or malformed upload. |
| `403` | Browser request has a foreign `Origin`; use the service's own origin. |
| `404` | Unknown library. |
| `409` | Duplicate refused, or the format is already present during a merge. |
| `413` | Request exceeds the upload limit (default 1024 MiB including multipart overhead), or the proxy's limit. |
| `502` | Calibre import failed or timed out; inspect the JSON `error`. |
| `503` | Calibre is unavailable or the library is locked by another Calibre program. |

Imports require the service user to have write and directory traversal access to the library,
plus a writable upload cache and an executable `calibredb`. Do not add while the Calibre desktop
app has the same library open. The service has no authentication and is exposed through the existing
LAN-only proxy.
