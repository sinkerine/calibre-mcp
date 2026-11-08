"""Bounded HTTP uploads and Calibre metadata overrides."""

from __future__ import annotations

import math
import re
import shutil
import tempfile
from collections import defaultdict
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from python_multipart.multipart import parse_options_header
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request

from .importer import DUPLICATES, AddError, BookMetadata, Duplicates


@dataclass
class Upload:
    path: Path
    metadata: BookMetadata
    duplicates: Duplicates


class UploadError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def safe_filename(name: str) -> str:
    """Keep a sanitised basename with a non-empty stem and extension."""
    name = re.split(r"[/\\]", name)[-1]
    name = re.sub(r'[\x00-\x1f\x7f-\x9f/:*?"<>|]', "", name).strip(" .")
    stem, dot, extension = name.rpartition(".")
    stem = stem.strip(" .")
    if not dot or not stem or not extension:
        raise UploadError("filename must have a non-empty stem and extension")
    return f"{stem[:150]}.{extension}"


def metadata_from(fields: Mapping[str, list[str]]) -> tuple[BookMetadata, Duplicates]:
    """Parse repeated form or query fields into validated metadata overrides."""
    def one(key: str) -> str | None:
        values = fields.get(key, [])
        if len(values) > 1:
            raise UploadError(f"{key} must be supplied only once")
        return values[0] if values else None

    def split(key: str, separator: str) -> list[str] | None:
        return [part.strip() for value in fields[key] for part in value.split(separator)] if key in fields else None

    duplicates = one("duplicates")
    if duplicates is None:
        duplicates = "refuse"
    if duplicates not in DUPLICATES:
        raise UploadError(f"unknown duplicates mode: {duplicates!r}")
    index = one("series_index")
    try:
        series_index = float(index) if index is not None else None
    except ValueError as exc:
        raise UploadError("series_index must be a number") from exc
    if series_index is not None and not math.isfinite(series_index):
        raise UploadError("series_index must be finite")
    identifiers = {}
    for value in fields.get("identifier", []):
        key, sep, val = value.partition(":")
        key = key.strip().lower()
        if not sep or key in identifiers:
            raise UploadError("identifiers must be distinct type:value pairs")
        identifiers[key] = val
    metadata = BookMetadata(title=one("title"), authors=split("authors", "&"), tags=split("tags", ","),
                            series=one("series"), series_index=series_index, languages=split("languages", ","),
                            identifiers=identifiers or None)
    try:
        metadata.args()
    except AddError as exc:
        raise UploadError(str(exc)) from exc
    return metadata, cast(Duplicates, duplicates)


@asynccontextmanager
async def receive(request: Request, upload_root: Path, max_bytes: int) -> AsyncIterator[Upload]:
    """Receive a bounded body and remove its temporary files on every exit."""
    message = f"upload larger than {max_bytes / (1024 * 1024):g} MB"
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError as exc:
            raise UploadError("invalid Content-Length") from exc
        if declared > max_bytes:
            raise UploadError(message, 413)

    async def capped_stream():
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise UploadError(message, 413)
            yield chunk

    upload_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".upload-", dir=upload_root))
    fields = defaultdict(list)
    try:
        if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            parser = MultiPartParser(request.headers, capped_stream(), max_files=1, max_fields=50)
            try:
                form = await parser.parse()
            except MultiPartException as exc:
                raise UploadError(str(exc)) from exc
            try:
                part = form.get("file")
                if not isinstance(part, UploadFile):
                    raise UploadError("missing file part")
                for key, value in form.multi_items():
                    if isinstance(value, str):
                        fields[key].append(value)
                name = fields.get("filename", [part.filename or ""])[-1]
                path = tmp / safe_filename(name)
                with path.open("wb") as dest:
                    while chunk := await part.read(1024 * 1024):
                        dest.write(chunk)
            finally:
                await form.close()
        else:
            for key, value in request.query_params.multi_items():
                fields[key].append(value)
            _, options = parse_options_header(request.headers.get("content-disposition", ""))
            name = request.query_params.get("filename") or options.get(b"filename", b"").decode("utf-8", "replace")
            path = tmp / safe_filename(name)
            with path.open("wb") as dest:
                async for chunk in capped_stream():
                    dest.write(chunk)
        if path.stat().st_size == 0:
            raise UploadError("empty upload")
        metadata, duplicates = metadata_from(fields)
        yield Upload(path, metadata, duplicates)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sweep(upload_root: Path) -> None:
    """Remove upload directories left behind by a crashed process."""
    for path in upload_root.glob(".upload-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
