"""Same input, same plan (2026-10-07).

KS-17185's requirement, run twice a few minutes apart, gave 2 Stories once and
4 the next. An identical input now gets the plan already written — no model
call — and any change to the input gets a fresh one.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from src.classification import plan_cache
from src.classification.decompose import GENERATOR_HEURISTIC, GENERATOR_LLM
from src.models.schemas import WorkBreakdown
from src.tools import tools as tool_module
from tests.test_local_pdf import small


@pytest.fixture
def cache_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("TASK_PLAN_CACHE_HOURS", "24")
    monkeypatch.setenv("TASK_PLAN_CACHE_DIR", str(tmp_path / "plans"))
    plan_cache._memory.clear()
    return tmp_path / "plans"


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The breakdown model, counted; and no Jira for the duplicate check."""
    calls: list[str] = []

    async def build(text, _context):
        calls.append(text)
        breakdown = WorkBreakdown.model_validate(small())
        breakdown._quality_notes = ["one note"]
        breakdown._coverage = {"1": "S1"}
        return breakdown, GENERATOR_LLM

    async def every_ticket(_project, _proposed, _text, existing):
        return existing

    monkeypatch.setattr(tool_module, "build_breakdown", build)
    monkeypatch.setattr(tool_module, "_every_ticket", every_ticket)
    return calls


def run(text: str, project: str = "BGV") -> dict:
    overrides = {"project": {"key": project, "name": "Praman"}, "existing_issues": []}
    return asyncio.run(tool_module._generate_work_breakdown(text, overrides))


def test_the_same_input_gets_the_same_plan_without_a_model_call(cache_on, model) -> None:
    first = run("Bulk print the latest consumer report PDFs from the dashboard.")
    second = run("Bulk print the latest consumer report PDFs from the dashboard.")

    assert len(model) == 1, "the second run used the kept plan"
    assert second["breakdown"] == first["breakdown"]
    assert second["plan_reused"] is True and first["plan_reused"] is False
    assert second["quality_notes"] == ["one note"] and second["coverage"] == {"1": "S1"}


def test_one_word_changed_writes_a_fresh_plan(cache_on, model) -> None:
    run("Bulk print the latest consumer report PDFs from the dashboard.")
    run("Bulk print the latest consumer report PDFs from the report dashboard.")
    assert len(model) == 2


def test_another_project_writes_a_fresh_plan(cache_on, model) -> None:
    run("Bulk print the latest consumer report PDFs.", project="BGV")
    run("Bulk print the latest consumer report PDFs.", project="FL")
    assert len(model) == 2


def test_a_kept_plan_survives_a_restart(cache_on: Path, model) -> None:
    run("Bulk print the latest consumer report PDFs.")
    plan_cache._memory.clear()  # a restarted worker
    run("Bulk print the latest consumer report PDFs.")
    assert len(model) == 1
    assert len(list(cache_on.glob("*.json"))) == 1


def test_a_plan_older_than_the_window_is_written_afresh(cache_on, model, monkeypatch) -> None:
    run("Bulk print the latest consumer report PDFs.")
    later = time.time() + 25 * 3600
    monkeypatch.setattr(plan_cache.time, "time", lambda: later)
    run("Bulk print the latest consumer report PDFs.")
    assert len(model) == 2


def test_with_the_window_at_zero_nothing_is_kept(monkeypatch, model) -> None:
    monkeypatch.setenv("TASK_PLAN_CACHE_HOURS", "0")
    run("Bulk print the latest consumer report PDFs.")
    run("Bulk print the latest consumer report PDFs.")
    assert len(model) == 2


def test_only_a_plan_the_model_wrote_is_kept(cache_on, monkeypatch) -> None:
    calls: list[str] = []

    async def heuristic(text, _context):
        calls.append(text)
        return WorkBreakdown.model_validate(small()), GENERATOR_HEURISTIC

    async def every_ticket(_project, _proposed, _text, existing):
        return existing

    monkeypatch.setattr(tool_module, "build_breakdown", heuristic)
    monkeypatch.setattr(tool_module, "_every_ticket", every_ticket)
    run("Bulk print the latest consumer report PDFs.")
    run("Bulk print the latest consumer report PDFs.")
    assert len(calls) == 2


def test_the_fingerprint_covers_text_project_and_model() -> None:
    base = dict(project_key="BGV", project_name="Praman", model="gpt-5.1")
    key = plan_cache.fingerprint("x", **base)
    assert key == plan_cache.fingerprint("x", **base)
    assert key != plan_cache.fingerprint("y", **base)
    assert key != plan_cache.fingerprint("x", **{**base, "model": "gpt-4o"})
    assert key != plan_cache.fingerprint("x", **{**base, "project_key": "FL"})


def test_a_long_requirement_is_condensed_once_for_the_same_input(cache_on, monkeypatch) -> None:
    calls: list[int] = []

    async def fit(text, *, chat, budget=None):
        calls.append(len(text))
        return "condensed points", ["condensed by the model"]

    monkeypatch.setenv("TASK_CONTEXT_MAX_CHARS", "1000")
    monkeypatch.setattr(tool_module, "fit_requirement", fit)
    fn = getattr(tool_module.condense_requirement, "__wrapped__", tool_module.condense_requirement)
    long_text = "a transcript line about bulk printing. " * 100
    first = asyncio.run(fn(long_text))
    second = asyncio.run(fn(long_text))
    assert len(calls) == 1 and second == first == {
        "requirement": "condensed points",
        "notes": ["condensed by the model"],
    }
