"""Files uploaded on the agent's own Aetherion form (2026-09-30).

The hosted form stores every picked file in the team's object storage and
passes their keys in ``uploaded_files`` — the keys of every file field, in the
form's order (``aetherion_sdk.local_form``). The bucket is the team: the
payload's ``team_id``, else ``TENANT_ID``, as for the Review agent's transcript.

Each file is read exactly like a Jira attachment (``attachments.read_all``):
the same types, caps and parallel reads, and one that cannot be read comes
back named with the reason, never silently dropped.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from src.context.attachments import read_all
from src.context.ingest import _section
from src.models.schemas import AttachmentText

HEADING_UPLOADS = "Uploaded on the Aetherion form (supporting material)"
NO_STORAGE = (
    "Uploaded on the form, but this deployment has no object storage set "
    "(TENANT_ID), so it could not be read. Attach it to the Jira ticket instead."
)


def upload_keys(fields: dict[str, Any]) -> list[str]:
    """The storage keys in the payload's upload fields, in order, without repeats.

    ``uploaded_files`` is a list of keys; a comma-separated string, or entries
    shaped ``{"key": …}``, are accepted too, so a change in how the platform
    sends them loses nothing.
    """
    keys: list[str] = []
    for value in fields.values():
        items = value if isinstance(value, list) else str(value or "").split(",")
        for item in items:
            key = str((item.get("key") if isinstance(item, dict) else item) or "").strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def file_name(key: str) -> str:
    return key.rstrip("/").rsplit("/", 1)[-1] or key


def bucket(team_id: str | None) -> str:
    return (team_id or "").strip() or os.environ.get("TENANT_ID", "").strip()


def _retrieve(bucket_name: str, key: str) -> bytes:
    # Imported where used: only a run with uploads needs object storage.
    from common_lib.storage.storage_client import RetrievalMode, storage

    storage.init_client()
    data = storage.retrieve(bucket_name, key, RetrievalMode.FULL_OBJECT)
    return bytes(data) if isinstance(data, bytes | bytearray) else str(data).encode("utf-8")


async def read_uploads(
    keys: list[str], team_id: str | None
) -> tuple[list[AttachmentText], list[str]]:
    """Each uploaded file's text (or why it could not be read), plus notes for the reply."""
    if not keys:
        return [], []
    where = bucket(team_id)
    if not where:
        return [AttachmentText(filename=file_name(k), note=NO_STORAGE) for k in keys], []

    async def download(key: str) -> bytes:
        return await asyncio.to_thread(_retrieve, where, key)

    return await read_all([{"filename": file_name(k), "content": k} for k in keys], download)


def section(files: list[AttachmentText]) -> str:
    """The uploaded files as one part of the requirement, like attached documents."""
    parts = [f"### Attachment: {f.filename}\n{f.text.strip()}" for f in files if f.text.strip()]
    if not parts:
        return ""
    return _section(HEADING_UPLOADS, "\n\n".join(parts))
