"""Files uploaded on the Review form (upload, bucket-gated): transcripts, specs …

Reads each uploaded file from object storage — several may be uploaded at once.
Storage is OPTIONAL: with no bucket (``team_id``/``TENANT_ID``) the source
degrades to empty with a warning.
``common_lib`` / ``agent_lib`` are imported lazily so this module stays importable
(and testable) without the SDK.
"""

from __future__ import annotations

import logging

from config import MAX_TRANSCRIPT_CHARS
from shared.transcripts import clean_transcript, is_transcript

from .base import ContextDocument, ContextSource, FetchContext

logger = logging.getLogger(__name__)

_TEXT_EXTENSIONS = (".txt", ".md", ".csv", ".log", ".json")


class TranscriptSource(ContextSource):
    """Every file uploaded on the form: a meeting transcript, a spec, notes …"""

    source_id = "transcript"

    def is_enabled(self, ctx: FetchContext) -> bool:
        return bool(ctx.transcript_file_keys)

    async def load(self, ctx: FetchContext) -> list[ContextDocument]:
        keys = ctx.transcript_file_keys
        if not keys:
            return []
        if not ctx.team_id:
            ctx.warnings.append(
                "file(s) uploaded but no bucket/team_id (TENANT_ID) set; skipping "
                + ", ".join(_name(k) for k in keys)
            )
            logger.warning("No team_id/TENANT_ID; cannot read uploads %s", keys)
            return []
        docs = [await self._load_one(ctx, key) for key in keys]
        return [doc for doc in docs if doc is not None]

    async def _load_one(self, ctx: FetchContext, key: str) -> ContextDocument | None:
        name = _name(key)
        try:
            text = await self._read(ctx.team_id, key)
        except Exception as e:
            ctx.warnings.append(f"uploaded file {name} could not be read: {e}")
            logger.warning("Upload read failed for %s: %s", key, e, exc_info=True)
            return None

        text = (text or "").strip()
        if not text:
            ctx.warnings.append(f"uploaded file {name} was empty")
            return None

        truncated = text[:MAX_TRANSCRIPT_CHARS]
        if len(text) > MAX_TRANSCRIPT_CHARS:
            truncated += "\n…[truncated]"
        return ContextDocument(
            source_id="transcript",
            # A transcript keeps its familiar label; any other file is named.
            source_label="Meeting Transcript" if is_transcript(key) else f"Uploaded file: {name}",
            kind="transcript",
            text=truncated,
            metadata={"file_key": key, "filename": name},
        )

    async def _read(self, team_id: str, key: str) -> str:
        # Lazy imports — only needed when an actual transcript is read.
        from common_lib.storage.storage_client import RetrievalMode, storage

        storage.init_client()
        data = storage.retrieve(team_id, key, RetrievalMode.FULL_OBJECT)

        if isinstance(data, bytes | bytearray):
            if is_transcript(key):
                return clean_transcript(bytes(data).decode("utf-8-sig", errors="replace"))
            if key.lower().endswith(_TEXT_EXTENSIONS):
                return bytes(data).decode("utf-8", errors="replace")
            # Binary (e.g. .docx/.pdf) — use agent_lib's extractor, fall back to decode.
            try:
                from agent_lib.utils.file_utils import fetch_reference_data
                from common_lib.models.embeddings import Reference

                return fetch_reference_data(
                    Reference(agent="demo_pranil", filename=key, file_content=bytes(data))
                )
            except Exception as e:
                logger.warning("Binary transcript extract failed (%s); decoding as utf-8", e)
                return bytes(data).decode("utf-8", errors="replace")
        return str(data)


def _name(key: str) -> str:
    return key.rstrip("/").rsplit("/", 1)[-1] or key
