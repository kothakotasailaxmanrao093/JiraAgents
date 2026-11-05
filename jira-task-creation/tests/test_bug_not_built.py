"""A bug is investigated, never broken into work items (2026-10-22).

"@Aetherion build" on a Bug created Sub-tasks under it, and a description of
something broken became a Task with Sub-tasks. The router now sends bugs to the
Review agent's bug investigation; these tests pin this agent's own safety net
for when a bug reaches it anyway (a direct run, the Run form, an older router).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agent import agent as agent_module
from src.agent.agent import JiraTaskCreation
from src.agent.bug_rules import BUG_HEADLINE, bug_refusal, is_bug_type
from src.models.schemas import ResultStatus
from tests.test_agent_webhook import DEFAULTS, Recorder, jira_context, source_issue
from tests.test_root_ticket import READY, breakdown_result, root_source


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch):
    def install(**results: Any) -> Recorder:
        recorder = Recorder(results)
        monkeypatch.setattr(agent_module.toolExecutor, "execute", recorder.execute)
        return recorder

    return install


def test_bug_types_are_recognised() -> None:
    for name in ("Bug", "bug", " Defect "):
        assert is_bug_type(name)
    for name in ("Story", "Task", "Epic", "Sub-task", "", None):
        assert not is_bug_type(name)


def test_the_refusal_offers_the_next_steps() -> None:
    reason, options = bug_refusal("BTS-30")
    assert "BTS-30 is a Bug" in reason and "nothing was changed" in reason
    joined = " ".join(options)
    assert "investigate" in joined and "create TDD" in joined and "build" in joined


async def test_build_on_a_bug_card_creates_nothing_before_any_model_call(tools) -> None:
    recorder = tools(read_jira_issue=root_source(key="BTS-30", issue_type="Bug"), **DEFAULTS)

    result = await JiraTaskCreation.fn({"issue_key": "BTS-30"})

    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert "BTS-30 is a Bug" in result["message"]
    assert not recorder.ran("validate_requirement")
    assert not recorder.ran("generate_work_breakdown")
    assert not recorder.ran("create_jira_issues")


async def test_a_delegated_build_on_a_bug_returns_nothing_created(tools) -> None:
    tools(read_jira_issue=root_source(key="BTS-30", issue_type="Bug"), **DEFAULTS)

    result = await JiraTaskCreation.fn({"issue_key": "BTS-30", "mode": "delegated"})

    assert result["outcome"] == "NEEDS_INFO"
    assert result["headline"] == BUG_HEADLINE
    assert result["created"] == []


async def test_a_described_defect_is_not_broken_down(tools) -> None:
    defect = breakdown_result()
    defect["breakdown"] = {**defect["breakdown"], "work_kind": "defect"}
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement=READY,
        generate_work_breakdown=defect,
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert "describes something that is broken" in result["message"]
    assert not recorder.ran("create_jira_issues")


async def test_a_feature_on_a_story_card_is_still_built(tools) -> None:
    created = {"status": ResultStatus.JIRA_CREATED.value, "created_keys": ["KS-30"]}
    recorder = tools(
        read_jira_issue=root_source(issue_type="Story"),
        inspect_jira_context=jira_context(),
        validate_requirement=READY,
        generate_work_breakdown=breakdown_result(),
        create_jira_issues=created,
        **DEFAULTS,
    )

    await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert recorder.ran("create_jira_issues")
