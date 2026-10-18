"""Short-lived storage for generated PDFs.

A document is written once under `<GENERATED_PDF_DIR>/<user id>/<uuid>__<name>.pdf`, downloaded straight
away by the web app, and swept by `delete_expired()`, which the notification worker runs every 10 minutes.
The folder is per user, so a download is only ever looked up inside the caller's own folder."""

import os
import re
import time
import uuid
from pathlib import Path

from django.conf import settings

# Characters kept in the stored file name; the real, display file name travels in the API response.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def storage_dir():
    return Path(getattr(settings, "GENERATED_PDF_DIR", Path(settings.BASE_DIR) / "generated_pdfs"))


def ttl_seconds():
    return int(getattr(settings, "GENERATED_PDF_TTL_SECONDS", 300))


def store_pdf(user, filename, data):
    """Write the PDF and return its id and display file name."""
    file_id = uuid.uuid4()
    folder = storage_dir() / str(user.pk)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{file_id}__{_UNSAFE.sub('_', filename)}"
    partial = folder / f"{path.name}.part"
    partial.write_bytes(data)
    os.replace(partial, path)  # a download never sees a half-written file
    return {"id": str(file_id), "filename": filename}


def find_pdf(user, file_id):
    """The stored file for this user, as (path, display name), or None."""
    try:
        file_id = uuid.UUID(str(file_id))
    except ValueError:
        return None
    folder = storage_dir() / str(user.pk)
    for path in folder.glob(f"{file_id}__*.pdf"):
        return path, path.name.split("__", 1)[1]
    return None


def delete_expired(max_age_seconds=None, now=None):
    """Delete stored PDFs (and abandoned partial writes) older than the limit. Returns how many were removed."""
    limit = ttl_seconds() if max_age_seconds is None else max_age_seconds
    now = time.time() if now is None else now
    root = storage_dir()
    removed = 0
    if not root.is_dir():
        return removed
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            try:
                if path.is_file() and now - path.stat().st_mtime >= limit:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue  # removed by a concurrent sweep
        try:
            folder.rmdir()  # only succeeds when empty
        except OSError:
            pass
    return removed
