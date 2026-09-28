"""Requirements-meeting transcript source (file upload, bucket-gated).

Reads the uploaded transcript from object storage. Storage is OPTIONAL: with no
bucket (``team_id``/``TENANT_ID``) the source degrades to empty with a warning.
``common_lib`` / ``agent_lib`` are imported lazily so this module stays importable
(and testable) without the SDK.
"""

from __future__ import annotations

import logging

from config import MAX_TRANSCRIPT_CHARS

from .base import ContextDocument, ContextSource, FetchContext

logger = logging.getLogger(__name__)

_TEXT_EXTENSIONS = (".txt", ".vtt", ".md", ".csv", ".log", ".srt", ".json")


class TranscriptSource(ContextSource):
    source_id = "transcript"

    def is_enabled(self, ctx: FetchContext) -> bool:
        return bool(ctx.transcript_file_key)

    async def load(self, ctx: FetchContext) -> list[ContextDocument]:
        key = ctx.transcript_file_key
        if not key:
            return []
        if not ctx.team_id:
            ctx.warnings.append(
                "transcript file provided but no bucket/team_id (TENANT_ID) set; "
                "skipping transcript"
            )
            logger.warning("No team_id/TENANT_ID; cannot read transcript %s", key)
            return []

        try:
            text = await self._read(ctx.team_id, key)
        except Exception as e:
            ctx.warnings.append(f"transcript read failed: {e}")
            logger.warning("Transcript read failed for %s: %s", key, e, exc_info=True)
            return []

        text = (text or "").strip()
        if not text:
            ctx.warnings.append("transcript was empty")
            return []

        truncated = text[:MAX_TRANSCRIPT_CHARS]
        if len(text) > MAX_TRANSCRIPT_CHARS:
            truncated += "\n…[truncated]"
        return [
            ContextDocument(
                source_id="transcript",
                source_label="Meeting Transcript",
                kind="transcript",
                text=truncated,
                metadata={"file_key": key},
            )
        ]

    async def _read(self, team_id: str, key: str) -> str:
        # Lazy imports — only needed when an actual transcript is read.
        from common_lib.storage.storage_client import RetrievalMode, storage

        storage.init_client()
        data = storage.retrieve(team_id, key, RetrievalMode.FULL_OBJECT)

        if isinstance(data, bytes | bytearray):
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
