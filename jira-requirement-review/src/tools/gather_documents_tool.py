"""@tool: the documents to review when there is no Jira ticket (2026-10-08).

A request started on the orchestrator's form writes nothing to Jira: Task
Creation's breakdown PDF — with the files the person attached — is what is
reviewed. The same happens on Review's own form when only files are uploaded.

Each file is read from object storage the way an uploaded transcript or spec
always was (``TranscriptSource``); only the labels change, so the model cites
"Document: <name>" and every file counts as carrying the requirement.
"""

from __future__ import annotations

import logging
from typing import Any

from aetherion_sdk import tool

from config import resolve_team_id
from context.base import FetchContext
from context.budget import trim_documents_to_budget
from context.transcript_source import TranscriptSource, _name
from shared.transcripts import is_transcript

logger = logging.getLogger(__name__)


def as_review_documents(loaded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Uploaded files relabelled as the requirement under review."""
    out = []
    for doc in loaded:
        key = str((doc.get("metadata") or {}).get("file_key") or "")
        out.append(
            {
                **doc,
                "source_id": "document",
                "source_label": f"Document: {_name(key)}" if key else doc.get("source_label", ""),
                "kind": "transcript" if is_transcript(key) else "requirement",
            }
        )
    return out


@tool(name="gather_documents")
async def gather_documents(
    file_keys: list[str], title: str = "", team_id: str | None = None
) -> dict[str, Any]:
    """Read the files to review; the same shape ``gather_context`` returns."""
    keys = [k for k in file_keys or [] if str(k).strip()]
    title = title.strip() or "Document review"
    ctx = FetchContext(issue_key=title, transcript_file_keys=keys, team_id=resolve_team_id(team_id))
    loaded = [d.to_dict() for d in await TranscriptSource().load(ctx)]
    documents, trim_warning = trim_documents_to_budget(as_review_documents(loaded))
    if trim_warning:
        ctx.warnings.append(trim_warning)
    logger.info("gather_documents: %d of %d file(s) read", len(documents), len(keys))
    return {
        "documents": documents,
        "target_bundle": {"key": title, "summary": title, "document_review": True},
        "has_requirement": any((d.get("text") or "").strip() for d in documents),
        "issue_summary": title,
        "counts": {"documents": len(documents), "attachments_total": len(keys)},
        "warnings": ctx.warnings,
    }
