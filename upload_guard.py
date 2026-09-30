"""
Upload validation (S-5.5): type, size AND content.

The file extension is attacker-controlled; what decides is the content. A file
saved under /static/uploads is served from OUR origin, so an ``.html`` or
``.svg`` slipped in as an "image" would run script with our cookies (stored
XSS). Hence:

  * images are accepted only when the leading bytes are a real PNG/JPEG/GIF/WEBP
    and are stored under a random server-generated name with the canonical
    extension for what they really are (never the client's name);
  * documents (CV) must match a known signature when they are binary, and text
    uploads must be valid UTF-8/Latin-1 text without NUL bytes;
  * size is checked against the real byte count, not a header.
"""

import os
import uuid

IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)

TEXT_EXTS = {".txt", ".md", ".markdown", ".text", ".csv", ".rtf"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".heic", ".heif"}


class UploadRejected(Exception):
    """User-facing (Danish) reason in ``str(exc)``."""


def sniff_image(data):
    """Canonical extension for real image content, else None."""
    head = data[:16]
    for sig, ext in IMAGE_SIGNATURES:
        if head.startswith(sig):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def looks_like(data, ext):
    """Does ``data`` plausibly match the claimed ``ext`` for CV uploads?"""
    ext = (ext or "").lower()
    if ext == ".pdf":
        return data[:1024].lstrip().startswith(b"%PDF-")
    if ext in IMAGE_EXTS:
        if ext in (".bmp",):
            return data[:2] == b"BM"
        if ext in (".tiff",):
            return data[:4] in (b"II*\x00", b"MM\x00*")
        if ext in (".heic", ".heif"):
            return data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypheif", b"ftyphevc")
        return sniff_image(data) is not None
    if ext in TEXT_EXTS or ext == "":
        if b"\x00" in data[:8192]:
            return False
        head = data[:4096].lstrip().lower()
        # text uploads that are really markup/script are not CVs
        if head.startswith((b"<!doctype", b"<html", b"<script", b"<svg", b"<?xml", b"<?php")):
            return False
        return True
    return False


def read_limited(file_storage, max_bytes):
    """Read the upload fully, refusing more than ``max_bytes``."""
    try:
        file_storage.stream.seek(0)
    except Exception:
        pass
    data = file_storage.read(max_bytes + 1) or b""
    if len(data) > max_bytes:
        raise UploadRejected("Filen er for stor (maks %d MB)." % max(1, max_bytes // (1024 * 1024)))
    return data


def save_image(file_storage, folder, *, max_bytes=2 * 1024 * 1024, prefix=""):
    """Validate and store an image; returns the stored file name (not the path).
    Raises UploadRejected with a Danish message."""
    data = read_limited(file_storage, max_bytes)
    if not data:
        raise UploadRejected("Filen er tom.")
    ext = sniff_image(data)
    if not ext:
        raise UploadRejected("Kun billeder (PNG, JPG, GIF eller WebP) er tilladt.")
    os.makedirs(folder, exist_ok=True)
    name = "%s%s.%s" % (prefix, uuid.uuid4().hex, ext)
    with open(os.path.join(folder, name), "wb") as fh:
        fh.write(data)
    return name
