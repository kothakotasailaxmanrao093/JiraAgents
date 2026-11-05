"""Tool — local_step: one step of the local flow (2026-11-03). The work itself is
scripts/local_run.py's, the same the terminal commands run."""

from __future__ import annotations

import sys
from pathlib import Path

from aetherion_sdk import tool

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))


@tool()
async def local_step(
    step: str,
    issue_key: str,
    file_keys: list[str] | None = None,
    branch_name: str = "",
    target_branch: str = "",
    answers: str = "",
    services: str = "",
) -> dict:
    """review / plan / memory take the card's key; review takes the answers
    typed on the form and documents holding answers; plan takes the review
    report and other files (optional); code takes the TDD PDF, the new
    branch's name and the branch its pull requests go into (both optional)."""
    import local_run

    files = list(file_keys or [])
    if step == "code":
        pdfs = [k for k in files if k.lower().endswith(".pdf")]
        if not pdfs:
            return {"status": "error", "message": "Attach the TDD PDF Planning made to code it."}
        return await local_run.code(pdfs[0], branch_name, target_branch)
    if not issue_key:
        return {"status": "error", "message": "Give the card's key, e.g. BTS-41."}
    if step == "review":
        return await local_run.review(issue_key, answers, files)
    if step == "plan":
        return await local_run.plan(issue_key, files, services)
    return await local_run.memory(issue_key)
