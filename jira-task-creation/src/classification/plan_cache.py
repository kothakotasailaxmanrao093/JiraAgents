"""Same input, same plan (2026-10-07).

The breakdown model is not repeatable: KS-17185's requirement run twice, a few
minutes apart, gave 2 Stories once and 4 the next. A plan is therefore kept by
a fingerprint of exactly what it was written from — the requirement text (which
carries every attached file's contents), the project, and the model — and an
identical input gets that same plan back at once, with no model call.

Change one word, one file, the project or the model and the fingerprint
changes: a fresh plan is written. Only plans the model wrote and the checks
passed are kept. The duplicate check is never cached — Jira changes.

Kept for ``TASK_PLAN_CACHE_HOURS`` (24). In ``TASK_PLAN_CACHE_DIR`` when set
(the local launcher sets it, so plans survive a restart), else in memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from common_lib.utils.logger import setup_logger

logger = setup_logger(__name__)

HOURS_ENV = "TASK_PLAN_CACHE_HOURS"
DIR_ENV = "TASK_PLAN_CACHE_DIR"
DEFAULT_HOURS = 24.0

_memory: dict[str, dict[str, Any]] = {}


def ttl_seconds() -> float:
    raw = os.environ.get(HOURS_ENV, "").strip()
    try:
        hours = float(raw) if raw else DEFAULT_HOURS
    except ValueError:
        hours = DEFAULT_HOURS
    return max(0.0, hours) * 3600


def fingerprint(
    requirement: str, *, project_key: str, project_name: str, model: str, card: str = ""
) -> str:
    """What the plan was written from, as one hash (the card's type fixes its shape)."""
    basis = json.dumps(
        {
            "requirement": requirement,
            "project": project_key.upper(),
            "project_name": project_name,
            "model": model,
            "card": card,
        },
        sort_keys=True,
    )
    return hashlib.sha256(basis.encode()).hexdigest()


def _file(key: str) -> Path | None:
    folder = os.environ.get(DIR_ENV, "").strip()
    return Path(folder).expanduser() / f"{key}.json" if folder else None


def get(key: str) -> dict[str, Any] | None:
    """The kept plan (``breakdown``, ``generator``, notes) for this fingerprint, if fresh."""
    if ttl_seconds() <= 0:
        return None
    entry = _memory.get(key)
    path = _file(key)
    if entry is None and path is not None and path.exists():
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            entry = None
    if not entry or time.time() - float(entry.get("saved_at", 0)) > ttl_seconds():
        return None
    return entry


def put(
    key: str,
    breakdown: dict[str, Any],
    generator: str,
    *,
    quality_notes: list[str] | None = None,
    coverage: dict[str, str] | None = None,
) -> None:
    """Keep a plan. Never raises: a plan not kept only costs a fresh one later."""
    if ttl_seconds() <= 0:
        return
    entry = {
        "breakdown": breakdown,
        "generator": generator,
        "quality_notes": list(quality_notes or []),
        "coverage": dict(coverage or {}),
        "saved_at": time.time(),
    }
    _memory[key] = entry
    path = _file(key)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry))
    except OSError as exc:
        logger.warning(f"Plan not kept on disk: {exc}")


def get_fitted(text: str, budget: int) -> dict[str, Any] | None:
    """A long requirement as it was condensed before (``requirement``, ``notes``).

    Condensing is model-written too: without this, the same long input could be
    condensed slightly differently, change the fingerprint and get a new plan.
    """
    return get(_fit_key(text, budget))


def put_fitted(text: str, budget: int, requirement: str, notes: list[str]) -> None:
    if ttl_seconds() <= 0:
        return
    entry = {"requirement": requirement, "notes": list(notes), "saved_at": time.time()}
    key = _fit_key(text, budget)
    _memory[key] = entry
    path = _file(key)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(entry))
        except OSError as exc:
            logger.warning(f"Condensed requirement not kept on disk: {exc}")


def _fit_key(text: str, budget: int) -> str:
    return "fit-" + hashlib.sha256(f"{budget}\n{text}".encode()).hexdigest()
