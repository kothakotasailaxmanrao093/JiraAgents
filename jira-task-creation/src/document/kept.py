"""The breakdown PDF, kept in the agents' shared storage for the next agent (2026-10-08).

When the orchestrator runs a request from its form, nothing is written to Jira:
Task Creation's PDF is the document Requirement Review reviews. So the PDF is
kept in the storage every agent of the team reads (``TENANT_ID``, the same
bucket Planning keeps its designs in) and its key is handed back.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime

from common_lib.utils.logger import setup_logger

logger = setup_logger(__name__)

FOLDER = "breakdowns"


def keep_document(file_name: str, data: bytes) -> str:
    """Store ``data``; its storage key, or "" when it could not be kept. Never raises."""
    bucket = os.environ.get("TENANT_ID", "").strip()
    if not bucket or not data:
        logger.warning("Breakdown document not kept: no storage configured (TENANT_ID)")
        return ""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = re.sub(r"[^\w.\-]+", "_", os.path.basename(file_name)) or "breakdown.pdf"
    try:
        from common_lib.storage.storage_client import storage

        storage.init_client()
        _, key = storage.store_object(bucket, f"{FOLDER}/{stamp}_{name}", data, "application/pdf")
    except Exception as exc:  # noqa: BLE001 — reported by the caller, never raised
        logger.warning(f"Breakdown document not kept in storage: {exc}")
        return ""
    logger.info(f"Breakdown document kept as {key}")
    return str(key)
