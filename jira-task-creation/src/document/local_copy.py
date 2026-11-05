"""A copy of each breakdown document on the laptop, on a local run only (2026-10-07).

On a laptop the PDF is also kept in ``docs-export/task-creation/`` (the launcher
sets ``TASK_DOCUMENT_DIR``), whether it was emailed or the tickets were created.
It costs no tokens: the PDF is drawn from the breakdown the model has already
written. On Aetherion nothing is saved — the flag below is set only by the
local launcher.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from common_lib.utils.logger import setup_logger

from src.shared import llm

logger = setup_logger(__name__)

DIR_ENV = "TASK_DOCUMENT_DIR"
DEFAULT_DIR = "breakdown-documents"


def save_local(file_name: str, data: bytes) -> str:
    """Write ``data`` on a laptop run and return its full path; "" anywhere else.

    Never raises: a document that cannot be saved must not fail the run.
    """
    if os.environ.get(llm.LOCAL_RUN_FLAG) != "1" or not data:
        return ""
    try:
        folder = Path(os.environ.get(DIR_ENV) or DEFAULT_DIR).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (re.sub(r"[^\w.\- ]+", "_", Path(file_name).name) or "breakdown.pdf")
        path.write_bytes(data)
    except OSError as exc:
        logger.warning(f"Breakdown document not saved locally: {exc}")
        return ""
    logger.info(f"Breakdown document saved: {path.resolve()} ({len(data):,} bytes)")
    return str(path.resolve())
