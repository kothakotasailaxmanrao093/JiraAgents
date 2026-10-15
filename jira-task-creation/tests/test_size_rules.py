"""Sizes and card types decide the tickets (owner decision, 2026-10-07).

No card (a requirement typed on the form): Small is one Task, Medium one Story,
Large an Epic with Stories — each with its Sub-tasks. On a card the card's own
type decides (see test_root_ticket.py) and the model is told so up front.
"""

from __future__ import annotations

from typing import Any

import pytest
from builders import issue_payload
from fakes import FakeClient, FakeResponse

from src.classification.validate import ProjectContext, card_structure
from src.jira import api as jira
from src.models.schemas import ResultStatus, WorkBreakdown
from src.tools.tools import create_jira_issues, read_jira_issue
from tests.test_agent_webhook import DEFAULTS, Recorder, breakdown, jira_context, source_issue
from tests.test_schemas import make_epic, make_story, make_subtask


def plan(size: str, subtasks: int = 3) -> dict:
    stories = [make_story(subtasks=[make_subtask(title=f"Step {i}") for i in range(subtasks)])]
    epic = None
    if size == "Large":
        epic = make_epic()
        stories = [make_story(title="Feature one"), make_story(title="Feature two")]
    return WorkBreakdown(
        classification=size, analysis="Sized by the owner's rules.", epic=epic, stories=stories
    ).model_dump(mode="json")


async def create(fake, breakdown: dict) -> dict:
    return await create_jira_issues(breakdown=breakdown, requirement="x", project_key="ABC")


def types_created(fake) -> list[str]:
    return [i["fields"]["issuetype"]["name"] for i in fake.created]


# --- no card: the size decides ---------------------------------------------------


async def test_small_is_one_task_with_its_sub_tasks(fake_jira, jira_env) -> None:
    fake = fake_jira(issue_types=["Epic", "Story", "Task", "Sub-task"])
    result = await create(fake, plan("Small", subtasks=3))
    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert types_created(fake) == ["Task", "Sub-task", "Sub-task", "Sub-task"]
    assert "1 Task" in result["summary"]


async def test_a_one_step_small_task_has_no_sub_task(fake_jira, jira_env) -> None:
    fake = fake_jira(issue_types=["Epic", "Story", "Task", "Sub-task"])
    await create(fake, plan("Small", subtasks=1))
    assert types_created(fake) == ["Task"]


async def test_medium_is_one_story_with_its_sub_tasks(fake_jira, jira_env) -> None:
    fake = fake_jira(issue_types=["Epic", "Story", "Task", "Sub-task"])
    result = await create(fake, plan("Medium", subtasks=3))
    assert types_created(fake) == ["Story", "Sub-task", "Sub-task", "Sub-task"]
    assert result["epic"] is None, "no Epic for Medium"


async def test_large_is_an_epic_with_stories(fake_jira, jira_env) -> None:
    fake = fake_jira(issue_types=["Epic", "Story", "Task", "Sub-task"])
    await create(fake, plan("Large"))
    created = types_created(fake)
    assert created[0] == "Epic" and created.count("Story") == 2


async def test_a_project_without_a_task_type_gets_a_story(fake_jira, jira_env) -> None:
    fake = fake_jira(issue_types=["Epic", "Story", "Sub-task"])
    result = await create(fake, plan("Small", subtasks=2))
    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert types_created(fake)[0] == "Story"


# --- on a card: the model is told the card's type fixes the structure -----------


@pytest.mark.parametrize("card", ["Story", "Task", "Bug"])
def test_a_story_task_or_bug_card_is_one_item(card: str) -> None:
    text = card_structure("KS-17185", card)
    assert f"KS-17185, which is a {card}" in text and "not changed" in text
    assert "exactly ONE story entry" in text and "no Epic" in text


def test_an_epic_card_gets_stories() -> None:
    text = card_structure("KS-9", "Epic")
    assert "Stories that go under KS-9" in text


def test_without_a_card_nothing_is_fixed() -> None:
    assert card_structure("", "") == ""
    assert "STRUCTURE" not in ProjectContext(name="Praman").as_prompt_text(for_breakdown=True)


def test_the_breakdown_prompt_carries_the_card() -> None:
    context = ProjectContext(name="Praman", card_key="KS-17185", card_type="Story")
    assert "STRUCTURE (fixed)" in context.as_prompt_text(for_breakdown=True)
    assert "STRUCTURE" not in context.as_prompt_text(), "triage is not told"
    # Even with no project context configured, the structure still reaches it.
    bare = ProjectContext(card_key="KS-1", card_type="Task")
    assert "STRUCTURE (fixed)" in bare.as_prompt_text(for_breakdown=True)


# --- the flow hands the card to the breakdown ------------------------------------


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch):
    from src.agent import agent as agent_module

    def install(**results: Any) -> Recorder:
        recorder = Recorder(results)
        monkeypatch.setattr(agent_module.toolExecutor, "execute", recorder.execute)
        return recorder

    return install


async def test_a_card_run_passes_the_cards_type_to_the_breakdown(tools) -> None:
    from src.agent.agent import JiraTaskCreation

    recorder = tools(
        read_jira_issue=source_issue(ticket_is_root=True, issue_type="Story"),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        **DEFAULTS,
    )
    await JiraTaskCreation.fn({"issue_key": "KS-12", "comment_id": "9001"})
    _, args, _ = recorder.call("generate_work_breakdown")
    assert args[1]["card_key"] == "KS-12" and args[1]["card_type"] == "Story"


async def test_new_tickets_from_a_comment_carry_no_card(tools) -> None:
    from src.agent.agent import JiraTaskCreation

    recorder = tools(
        read_jira_issue=source_issue(ticket_is_root=False, issue_type="Story"),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        **DEFAULTS,
    )
    await JiraTaskCreation.fn({"issue_key": "KS-12", "comment_id": "9001"})
    _, args, _ = recorder.call("generate_work_breakdown")
    assert "card_key" not in args[1]


# --- a run by hand (no comment) builds on the card --------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    def install(routes: dict) -> FakeClient:
        fake = FakeClient(routes)
        monkeypatch.setattr(jira, "jira_client", lambda *a, **k: fake)
        return fake

    return install


async def test_a_run_by_hand_with_no_comment_builds_on_the_card(jira_env, client) -> None:
    client({"/rest/api/3/issue/KS-12": FakeResponse(200, issue_payload(trigger=None))})
    result = await read_jira_issue("KS-12", False, False)
    assert result["ok"] and result["ticket_is_root"] is True
