"""A normal build in under a minute, without losing accuracy (2026-09-30).

The slow parts were one AI answer holding the whole breakdown, and tickets
created one after another. Now the breakdown is planned first and its Stories
written at the same time, tickets are created in parallel, the two yes/no
checks may use a quicker model, and a matched ticket is checked to still exist.
Every draft still goes through exactly the same checks as before.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from src.classification import decompose
from src.classification.decompose import llm_breakdown
from src.classification.validate import ProjectContext
from src.models.schemas import DuplicateMatch, ResultStatus
from src.tools import tools
from tests.test_details import _story
from tests.test_jira_service import REQUIREMENT, medium_breakdown

LINES = [
    "Each completed check adds a line to the client's monthly invoice.",
    "On the 1st of every month an invoice PDF is generated.",
    "The invoice is emailed to the client's billing contact.",
    "Clients can download past invoices from their portal.",
    "Unpaid invoices older than 30 days trigger a reminder email.",
    "The account manager is told about every reminder sent.",
]
REQ = "- " + "\n- ".join(LINES)
TITLES = ["Generate Monthly Invoices", "Send Invoice Reminders"]
PLAN = {
    "classification": "Medium",
    "analysis": "Invoicing and reminders are two capabilities.",
    "epic": {
        "business_objective": "Bill clients every month.",
        "scope": ["Invoices", "Reminders"],
        "out_of_scope": ["Not specified"],
        "priority": "High",
        "acceptance_criteria": ["Clients are billed monthly."],
        "jira_summary": "Monthly client invoicing",
    },
    "stories": [{"title": t, "focus": t} for t in TITLES],
    "coverage": {
        "1": TITLES[0],
        "2": TITLES[0],
        "3": TITLES[0],
        "4": TITLES[0],
        "5": TITLES[1],
        "6": TITLES[1],
    },
}


def story_json(title: str) -> dict[str, Any]:
    criteria = (
        [f"Given {LINES[i]}, when it happens, then it is done" for i in range(4)]
        if title == TITLES[0]
        else [f"Given {LINES[i]}, when it happens, then it is done" for i in (4, 5)]
    )
    return _story(title, criteria, ["Prepare the data", "Deliver the result"])


class Model:
    """Answers each kind of prompt; records them and how many ran at once."""

    def __init__(self, plan: dict | None = None) -> None:
        self.plan = PLAN if plan is None else plan
        self.prompts: list[str] = []
        self.in_flight = self.peak = 0

    async def chat(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if prompt.startswith("Check this proposed Jira breakdown"):
            return json.dumps({})
        if "Rewrite ONLY the completion criteria" in prompt or prompt.startswith("Some stated"):
            return json.dumps({"criteria": {}})
        if "Plan the breakdown of the requirement" in prompt:
            return json.dumps(self.plan)
        if prompt.startswith("Write ONE Story"):
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            await asyncio.sleep(0.02)
            self.in_flight -= 1
            title = prompt.split("WRITE THIS ONE: ", 1)[1].split("\n", 1)[0]
            return json.dumps(story_json(title))
        # The one-call breakdown (the fallback).
        return json.dumps(
            {
                **{k: PLAN[k] for k in ("classification", "analysis", "epic")},
                "stories": [story_json(t) for t in TITLES],
                "coverage": PLAN["coverage"],
            }
        )

    def count(self, starts: str) -> int:
        return sum(1 for p in self.prompts if starts in p)


@pytest.fixture
def model(monkeypatch) -> Model:
    monkeypatch.setenv("LTW_PARALLEL_BREAKDOWN", "true")
    fake = Model()
    monkeypatch.setattr(decompose, "_chat", fake.chat)
    return fake


# --- the breakdown: plan, then every Story at once ----------------------------------------


async def test_a_plan_then_every_story_written_at_the_same_time(model) -> None:
    breakdown = await llm_breakdown(REQ, ProjectContext())
    assert [s.title for s in breakdown.stories] == TITLES
    assert model.count("Plan the breakdown") == 1
    assert model.count("Write ONE Story") == 2
    assert model.peak == 2, "the Stories were written in parallel"
    assert model.count("Decompose the requirement") == 0, "no one-call breakdown was needed"


async def test_an_unusable_plan_falls_back_to_the_one_call_breakdown(monkeypatch) -> None:
    monkeypatch.setenv("LTW_PARALLEL_BREAKDOWN", "true")
    fake = Model(plan={"classification": "Medium", "stories": []})
    monkeypatch.setattr(decompose, "_chat", fake.chat)
    breakdown = await llm_breakdown(REQ, ProjectContext())
    assert [s.title for s in breakdown.stories] == TITLES
    assert fake.count("Decompose the requirement") == 1


async def test_a_plan_missing_a_line_is_not_accepted(monkeypatch) -> None:
    """Accuracy first: a line no Story delivers sends it back to the one call."""
    monkeypatch.setenv("LTW_PARALLEL_BREAKDOWN", "true")
    gap = {**PLAN, "coverage": {k: v for k, v in PLAN["coverage"].items() if k != "6"}}
    fake = Model(plan=gap)
    monkeypatch.setattr(decompose, "_chat", fake.chat)
    await llm_breakdown(REQ, ProjectContext())
    assert fake.count("Decompose the requirement") == 1


async def test_a_short_requirement_keeps_the_single_call(model) -> None:
    short = "- " + "\n- ".join(LINES[:3])
    try:
        await llm_breakdown(short, ProjectContext())
    except Exception:  # the stand-in answer need not fit a 3-line requirement
        pass
    assert model.count("Plan the breakdown") == 0


# --- the quick model, only for the yes/no checks ------------------------------------------


async def test_the_fast_model_answers_only_triage_and_relatedness(monkeypatch) -> None:
    monkeypatch.setenv("LTW_LLM_FAST_MODEL", "gpt-4o-mini")
    used: list[tuple[str, str]] = []

    async def gateway(prompt: str, model: str) -> str:
        used.append((prompt.split("\n", 1)[0][:30], model))
        return json.dumps({"verdict": "VALID", "reason": "ok", "questions": [], "related": True})

    monkeypatch.setattr(decompose, "_gateway_chat", gateway)
    monkeypatch.setattr(decompose, "_chat", lambda p: gateway(p, "gpt-4o"))  # the main model
    await decompose.llm_triage("Let recruiters send reminders.", ProjectContext())
    await decompose.llm_related("BGV-1", "Reminders", "", "Send reminders")
    await decompose._chat("the breakdown itself")
    assert [m for _, m in used] == ["gpt-4o-mini", "gpt-4o-mini", "gpt-4o"]


async def test_a_failing_fast_model_hands_over_to_the_main_one(monkeypatch) -> None:
    monkeypatch.setenv("LTW_LLM_FAST_MODEL", "no-such-model")
    used: list[str] = []

    async def gateway(prompt: str, model: str) -> str:
        used.append(model)
        if model == "no-such-model":
            raise RuntimeError("404 model not found")
        return json.dumps({"verdict": "VALID", "reason": "ok", "questions": []})

    monkeypatch.setattr(decompose, "_gateway_chat", gateway)
    monkeypatch.setattr(decompose, "_chat", lambda p: gateway(p, "gpt-4o"))  # the main model
    await decompose.llm_triage("Let recruiters send reminders.", ProjectContext())
    assert used == ["no-such-model", "gpt-4o"]


# --- tickets created in parallel ----------------------------------------------------------


def slow_writes(monkeypatch, fake) -> dict[str, int]:
    seen = {"now": 0, "peak": 0}
    real = fake.post

    async def post(url: str, **kwargs: Any):
        seen["now"] += 1
        seen["peak"] = max(seen["peak"], seen["now"])
        await asyncio.sleep(0.01)
        seen["now"] -= 1
        return await real(url, **kwargs)

    monkeypatch.setattr(fake, "post", post)
    return seen


async def test_tickets_are_created_in_parallel_within_the_limit(fake_jira, jira_env, monkeypatch):
    fake = fake_jira()
    seen = slow_writes(monkeypatch, fake)
    result = await tools.create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)
    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert 1 < seen["peak"] <= 4
    epic = result["epic"]["key"]
    assert all(s["parent_key"] == epic for s in result["stories"])
    stories = {s["key"] for s in result["stories"]}
    assert all(t["parent_key"] in stories for t in result["subtasks"])


async def test_one_failing_story_still_reports_everything_made(fake_jira, jira_env, monkeypatch):
    fake = fake_jira(fail_on_summary="SMS notifications")
    result = await tools.create_jira_issues(breakdown=medium_breakdown(), requirement=REQUIREMENT)
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "SMS notifications" in result["failed_issue"]
    assert set(result["created_keys"]) == {i["key"] for i in fake.created}
    assert len(result["created_keys"]) > 1, "the other Stories were still created"


# --- a match must still exist -------------------------------------------------------------


async def test_a_deleted_ticket_never_blocks_new_work(fake_jira, jira_env) -> None:
    fake = fake_jira()
    fake.store.append({"key": "ABC-7", "fields": {"summary": "Send email"}, "labels": []})
    matches = [
        DuplicateMatch(
            proposed_title="Send email",
            existing_key="ABC-7",
            existing_summary="Send email",
            score=0.9,
        ),
        DuplicateMatch(
            proposed_title="Send SMS", existing_key="ABC-9", existing_summary="Send SMS", score=0.9
        ),
    ]
    kept = await tools._still_there(matches, "ABC")
    assert [m.existing_key for m in kept] == ["ABC-7"], "ABC-9 was deleted"
