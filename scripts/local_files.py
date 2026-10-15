"""Files picked on the local form, kept on this laptop (2026-10-06).

The local test form (``aetherion_sdk.local_form``) sends every picked file to
the HOSTED platform (``_upload_to_hosted``), and the agent then reads it back
from Aetherion's object storage. On a laptop both halves fail: the upload needs
a live ``aetherion login``, and the worker cannot reach that storage. Picked
files were silently left out of the run.

Here, on a laptop run only:

* ``install_form()`` — the form saves a picked file under ``.local-uploads/``
  and gives the run the key ``local-uploads/<id>/<name>``;
* ``install_storage()`` — a worker reading such a key reads that file; every
  other key goes to the platform's storage exactly as before.

Called by ``scripts/local_form.py`` and ``scripts/local_worker.py`` — never by an
agent — so the published agents and the hosted form are unchanged.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

PREFIX = "local-uploads/"
FOLDER_ENV = "LOCAL_UPLOADS_DIR"
DEFAULT_FOLDER = Path(__file__).resolve().parent.parent / ".local-uploads"


def folder() -> Path:
    return Path(os.environ.get(FOLDER_ENV) or DEFAULT_FOLDER)


def _safe_name(filename: str) -> str:
    """A file name that cannot leave its folder ("../x" becomes "x")."""
    name = Path(str(filename or "upload")).name
    return re.sub(r"[^\w.\- ()]+", "_", name).strip() or "upload"


def save(filename: str, data: bytes, content_type: str = "") -> dict:
    """Keep one picked file; return what the hosted upload returns (its ``key``)."""
    key = f"{PREFIX}{uuid.uuid4().hex[:12]}/{_safe_name(filename)}"
    path = folder() / key[len(PREFIX) :]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"local form: kept {filename!r} ({len(data):,} bytes) as {key}", flush=True)
    return {"key": key, "filename": _safe_name(filename), "content_type": content_type}


def read(key: str) -> bytes:
    """The bytes of a file kept by :func:`save`."""
    base = folder().resolve()
    path = (base / key[len(PREFIX) :]).resolve()
    if base not in path.parents:
        raise ValueError(f"not a local upload: {key!r}")
    return path.read_bytes()


async def save_upload(filename: str, data: bytes, content_type: str = "") -> dict:
    """:func:`save`, shaped as the form calls its upload: awaited."""
    return save(filename, data, content_type)


def install_form() -> None:
    """The local form keeps picked files here instead of uploading them."""
    from aetherion_sdk import local_form

    local_form._upload_to_hosted = save_upload


def install_storage() -> None:
    """A worker reads ``local-uploads/…`` keys from this laptop."""
    from common_lib.storage.storage_client import RetrievalMode, storage

    original_retrieve = storage.retrieve
    original_init = storage.init_client

    def init_client() -> None:
        try:
            original_init()
        except Exception as exc:  # noqa: BLE001 — no platform storage on a laptop is fine
            print(f"local storage: platform storage unavailable ({exc}); local files only")

    def retrieve(bucket_name, object_key, retrieval_mode=RetrievalMode.FULL_OBJECT):
        if str(object_key).startswith(PREFIX):
            return read(str(object_key))
        return original_retrieve(bucket_name, object_key, retrieval_mode)

    storage.init_client = init_client
    storage.retrieve = retrieve
