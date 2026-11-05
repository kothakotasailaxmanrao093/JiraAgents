"""Work is built on the ticket asked on, which is never changed (owner, 2026-10-07).

Replaces the 2026-09-28 design described below, where the description re-typed
the ticket and its summary and description were rewritten: now the card's own
type decides what goes under it, and the card itself is left exactly as it is.

(2026-09-28) The ticket asked on becomes the root of its own hierarchy.

"@Aetherion build" on BGV-25, a ticket holding a complete requirement, used to
leave BGV-25 as a bare trigger and create a new Epic that repeated it word for
word. Now BGV-25 itself becomes the Epic (or Story, Task or Bug — the
description decides), the work goes under it, and no Epic is created beside it.

The root is read and edited through the fake Jira in ``conftest.py``; the
fixtures are shaped as Jira Cloud sends them — the description is real ADF with
an embedded image, which must survive the rewrite untouched.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from src.agent import agent as agent_module
from src.agent.agent import JiraTaskCreation
from src.jira import root as root_mod
from src.models.schemas import ResultStatus, WorkBreakdown
from src.tools.tools import create_jira_issues
from tests.test_agent_webhook import DEFAULTS, Recorder, jira_context, source_issue
from tests.test_schemas import make_epic, make_story, make_subtask

TYPES = ["Epic", "Story", "Task", "Bug", "Subtask"]  # team-managed spelling, as BGV has

# BGV-25's description as Jira returns it: a paragraph and a pasted screenshot.
ORIGINAL = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "Monthly invoicing for every client."}],
        },
        {
            "type": "mediaSingle",
            "attrs": {"layout": "center"},
            "content": [
                {
                    "type": "media",
                    "attrs": {"id": "a1b2c3", "type": "file", "collection": "jira-10405"},
                }
            ],
        },
    ],
}


def root_spec(issuetype: str = "Task", **extra: Any) -> dict[str, Any]:
    return {
        "summary": "Monthly client invoicing",
        "description": copy.deepcopy(ORIGINAL),
        "issuetype": issuetype,
        "project": "BGV",
        "labels": [],
        **extra,
    }


def large() -> dict:
    return WorkBreakdown(
        classification="Large",
        analysis="Invoicing, emailing, portal access and reminders are separate workflows.",
        epic=make_epic(jira_summary="Automate Monthly Client Invoicing"),
        stories=[
            make_story(
                title="Generate Monthly Invoices for Completed Checks",
                subtasks=[make_subtask(title="Collect completed checks"), make_subtask()],
            ),
            make_story(
                title="Email Invoices to Client Billing Contacts",
                subtasks=[make_subtask(title="Send invoice email"), make_subtask()],
            ),
        ],
    ).model_dump(mode="json")


def small(work_kind: str = "feature", subtasks: int = 3, **extra: Any) -> dict:
    return WorkBreakdown(
        classification="Small",
        analysis="One focused capability with its own sub-tasks.",
        work_kind=work_kind,
        stories=[
            make_story(
                title="Verify Candidate ID Documents",
                subtasks=[make_subtask(title=f"Step {i}") for i in range(1, subtasks + 1)],
            )
        ],
        **extra,
    ).model_dump(mode="json")


PRODUCTION_PROBLEM = (
    "Since this morning's release, in production HR users cannot see the "
    "verification report. The page shows error 500. All clients are affected."
)


def defect(widespread: bool = True) -> dict:
    return small(
        work_kind="defect",
        defect={
            "actual": "The verification report page shows error 500.",
            "expected": "HR users see the verification report.",
            "impact": "Every client's HR users are blocked.",
            "widespread": widespread,
        },
    )


async def build(fake, breakdown: dict, requirement: str = "Monthly invoicing.") -> dict:
    return await create_jira_issues(
        breakdown=breakdown,
        requirement=requirement,
        project_key="BGV",
        root_key="BGV-25",
        idempotency_basis=requirement,
    )


def created_types(fake) -> list[str]:
    return [i["fields"]["issuetype"]["name"] for i in fake.created]


def parent_of(fake, key: str) -> str:
    issue = next(i for i in fake.created if i["key"] == key)
    return (issue["fields"].get("parent") or {}).get("key", "")


def headings(description: dict) -> list[str]:
    return [
        "".join(c.get("text", "") for c in n.get("content") or [])
        for n in description["content"]
        if n["type"] == "heading"
    ]


@pytest.fixture
def bgv(fake_jira, jira_env):
    def install(issuetype: str = "Task", **knobs: Any):
        spec = knobs.pop("spec", {})
        fake = fake_jira(issue_types=TYPES, epics={"BGV-25": root_spec(issuetype, **spec)}, **knobs)
        return fake

    return install


# --- the card's own type decides what goes under it (owner, 2026-10-07) -----------


def medium(subtasks: int = 3) -> dict:
    return WorkBreakdown(
        classification="Medium",
        analysis="One feature with several parts: one Story with a Sub-task per part.",
        stories=[
            make_story(
                title="Bulk print reports",
                subtasks=[make_subtask(title=f"Part {i}") for i in range(1, subtasks + 1)],
            )
        ],
    ).model_dump(mode="json")


def assert_card_untouched(fake, issuetype: str) -> None:
    card = fake.epics["BGV-25"]
    assert card["issuetype"] == issuetype, "the card keeps the type the person chose"
    assert card["summary"] == "Monthly client invoicing", "the summary is not rewritten"
    assert card["description"] == ORIGINAL, "the description is not rewritten"
    assert [key for key, _ in fake.edits if key == "BGV-25"] == [], "no edit of the card at all"


@pytest.mark.parametrize("breakdown", [small, medium], ids=["small", "medium"])
async def test_a_story_card_stays_a_story_and_gets_its_sub_tasks(bgv, breakdown) -> None:
    fake = bgv("Story")
    result = await build(fake, breakdown())

    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert created_types(fake) == ["Subtask", "Subtask", "Subtask"]
    assert all(parent_of(fake, i["key"]) == "BGV-25" for i in fake.created)
    assert "BGV-25 stays a Story; its summary and description were not changed." in (
        result["summary"]
    )
    assert "Created under it: 3 Sub-tasks." in result["summary"]
    assert result["root"]["type_before"] == result["root"]["type_after"] == "Story"
    assert_card_untouched(fake, "Story")


async def test_a_big_plan_on_a_story_card_puts_every_step_under_it(bgv) -> None:
    """The model was told the card is one Story; if it still splits the work,
    nothing is lost and the card still never becomes an Epic."""
    fake = bgv("Story")
    result = await build(fake, large())
    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert created_types(fake) == ["Subtask"] * 4, "no Epic, no Story — Sub-tasks only"
    assert all(parent_of(fake, i["key"]) == "BGV-25" for i in fake.created)
    assert_card_untouched(fake, "Story")


@pytest.mark.parametrize("card", ["Task", "Bug"])
async def test_a_task_or_bug_card_gets_sub_tasks_only_for_real_steps(bgv, card) -> None:
    fake = bgv(card)
    await build(fake, small(subtasks=2))
    assert created_types(fake) == ["Subtask", "Subtask"]
    assert_card_untouched(fake, card)

    fake = bgv(card)
    result = await build(fake, small(subtasks=1))
    assert fake.created == [], "one step needs no Sub-task"
    assert "It is one step, so no Sub-tasks were needed." in result["summary"]
    assert_card_untouched(fake, card)


async def test_a_bug_card_keeps_its_priority(bgv) -> None:
    fake = bgv("Bug")
    await build(fake, defect(), requirement=PRODUCTION_PROBLEM)
    assert "priority" not in fake.epics["BGV-25"] or fake.epics["BGV-25"].get("priority") in (
        None,
        "",
    )
    assert_card_untouched(fake, "Bug")


async def test_an_epic_card_stays_an_epic_and_gets_its_stories(bgv) -> None:
    fake = bgv("Epic")
    result = await build(fake, large())

    assert "Epic" not in created_types(fake), "no separate Epic"
    assert created_types(fake).count("Story") == 2
    stories = [i["key"] for i in fake.created if i["fields"]["issuetype"]["name"] == "Story"]
    assert all(parent_of(fake, k) == "BGV-25" for k in stories)
    for issue in fake.created:
        if issue["fields"]["issuetype"]["name"] == "Subtask":
            assert parent_of(fake, issue["key"]) in stories
    assert "BGV-25" not in result["created_keys"]
    assert "BGV-25 stays an Epic" in result["summary"]
    assert "No separate Epic was created." in result["summary"]
    assert_card_untouched(fake, "Epic")


async def test_an_epic_card_with_a_small_request_adds_one_story(bgv) -> None:
    fake = bgv("Epic")
    result = await build(fake, small())
    assert created_types(fake) == ["Story", "Subtask", "Subtask", "Subtask"]
    assert "BGV-25 stays an Epic" in result["summary"]
    assert_card_untouched(fake, "Epic")


def test_the_description_never_decides_the_cards_type() -> None:
    """2026-10-07: the plan follows the card's type, whatever the text says."""
    breakdown = WorkBreakdown.model_validate(defect())
    for card in ("Story", "Task", "Epic"):
        plan = root_mod.plan_root(breakdown, card)
        assert plan.role == card.lower()


# --- nothing is built unless every check passes ----------------------------------


async def test_a_sub_task_is_never_made_a_root(bgv) -> None:
    fake = bgv("Subtask", spec={"subtask": True})
    result = await build(fake, small())
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "is a Sub-task" in result["failure_reason"]
    assert fake.edits == [] and fake.created == []


async def test_no_edit_permission_is_needed_because_the_card_is_not_edited(bgv) -> None:
    fake = bgv("Story", can_edit=False)
    result = await build(fake, small())
    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert created_types(fake) == ["Subtask", "Subtask", "Subtask"]
    assert_card_untouched(fake, "Story")


# --- asking again ---------------------------------------------------------------------


async def test_building_on_the_same_card_twice_creates_nothing_the_second_time(bgv) -> None:
    fake = bgv("Epic")
    first = await build(fake, large())
    made = len(fake.created)

    second = await build(fake, large())

    assert len(fake.created) == made
    assert second["created_keys"] == []
    assert sorted(second["reused_keys"]) == sorted(first["created_keys"])
    assert_card_untouched(fake, "Epic")


async def test_a_run_that_stopped_part_way_continues(bgv) -> None:
    fake = bgv("Epic", fail_on_summary="Email Invoices")
    first = await build(fake, large())
    assert first["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "Building under BGV-25 stopped" in first["failure_reason"]
    made = [i["key"] for i in fake.created]

    fake.fail_on_summary = ""
    second = await build(fake, large())
    assert second["status"] == ResultStatus.JIRA_CREATED.value
    assert not set(second["created_keys"]) & set(made), "nothing made twice"
    assert set(made) <= set(second["reused_keys"])
    assert_card_untouched(fake, "Epic")


# --- the workflow: when the root is used, and when it is left alone -------------------------


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch):
    def install(**results: Any) -> Recorder:
        recorder = Recorder(results)
        monkeypatch.setattr(agent_module.toolExecutor, "execute", recorder.execute)
        return recorder

    return install


def root_source(**overrides: Any) -> dict[str, Any]:
    return source_issue(
        **{
            "key": "KS-12",
            "issue_type": "Task",
            "trigger_comment_body": "@Aetherion build",
            "trigger_is_pointer": True,
            "ticket_is_root": True,
            **overrides,
        }
    )


READY = {"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x", "request_text": "x"}


def breakdown_result(duplicates: list | None = None) -> dict:
    return {
        "breakdown": large(),
        "counts": {"epics": 1, "stories": 2, "subtasks": 4},
        "duplicate_matches": duplicates or [],
    }


async def test_the_root_key_reaches_creation_only_when_the_ticket_is_the_root(tools) -> None:
    created = {"status": ResultStatus.JIRA_CREATED.value, "created_keys": ["KS-30"]}
    recorder = tools(
        read_jira_issue=root_source(),
        inspect_jira_context=jira_context(),
        validate_requirement=READY,
        generate_work_breakdown=breakdown_result(),
        create_jira_issues=created,
        **DEFAULTS,
    )
    await JiraTaskCreation.fn({"issue_key": "KS-12"})
    _, args, _ = recorder.call("create_jira_issues")
    assert args[7] == "KS-12"

    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement=READY,
        generate_work_breakdown=breakdown_result(),
        create_jira_issues=created,
        **DEFAULTS,
    )
    await JiraTaskCreation.fn({"issue_key": "KS-12"})
    _, args, _ = recorder.call("create_jira_issues")
    assert args[7] == "", "a comment with its own requirement leaves the ticket alone"
    assert args[10] == "Priya", "the requester, for the origin line"


async def test_a_vague_requirement_never_reaches_the_root(tools) -> None:
    recorder = tools(
        read_jira_issue=root_source(),
        inspect_jira_context=jira_context(),
        validate_requirement={
            "status": ResultStatus.CLARIFICATION_REQUIRED.value,
            "reason": "Which reports?",
            "clarifying_questions": ["Which reports?"],
        },
        **DEFAULTS,
    )
    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})
    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert not recorder.ran("create_jira_issues"), "the only tool that edits the root"


async def test_existing_work_never_reaches_the_root(tools) -> None:
    duplicate = {
        "proposed_title": "Email Invoices to Client Billing Contacts",
        "existing_key": "KS-7",
        "existing_summary": "Email invoices to clients",
        "score": 0.9,
    }
    recorder = tools(
        read_jira_issue=root_source(),
        inspect_jira_context=jira_context(),
        validate_requirement=READY,
        generate_work_breakdown=breakdown_result([duplicate]),
        **DEFAULTS,
    )
    await JiraTaskCreation.fn({"issue_key": "KS-12"})
    assert not recorder.ran("create_jira_issues")


async def test_a_sub_task_root_is_refused_before_any_model_call(tools) -> None:
    recorder = tools(
        read_jira_issue=root_source(
            is_subtask=True,
            linked_issues=[{"key": "KS-10", "relationship": "parent of this issue"}],
        ),
        **DEFAULTS,
    )
    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})
    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert "cannot hold other tickets" in result["message"]
    assert "KS-10" in result["clarifying_questions"][0]
    assert not recorder.ran("generate_work_breakdown")
    assert not recorder.ran("create_jira_issues")


async def test_asking_again_on_a_built_root_reuses_it_without_the_model(tools) -> None:
    built = {
        "status": ResultStatus.JIRA_CREATED.value,
        "stories": [{"key": "KS-13", "issue_type": "Story", "summary": "Generate invoices"}],
        "reused_keys": ["KS-13"],
        "reused_existing": True,
    }
    recorder = tools(read_jira_issue=root_source(issue_type="Epic", root_built=built), **DEFAULTS)
    result = await JiraTaskCreation.fn({"issue_key": "KS-12", "mode": "delegated"})
    assert result["outcome"] == "ALREADY_EXISTS"
    assert result["headline"] == "KS-12 was already built — nothing created again"
    assert [d["existing_key"] for d in result["duplicates"]] == ["KS-13"]
    assert not recorder.ran("generate_work_breakdown")
    assert not recorder.ran("create_jira_issues")


async def test_the_reply_heading_says_what_was_created_under_the_card(tools) -> None:
    recorder = tools(
        read_jira_issue=root_source(),
        inspect_jira_context=jira_context(),
        validate_requirement=READY,
        generate_work_breakdown=breakdown_result(),
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["KS-13"],
            "stories": [{"key": "KS-13", "issue_type": "Story", "summary": "Generate"}],
            "root": {"key": "KS-12", "type_before": "Epic", "type_after": "Epic"},
            "summary": "KS-12 stays an Epic; its summary and description were not changed.",
        },
        **DEFAULTS,
    )
    result = await JiraTaskCreation.fn({"issue_key": "KS-12", "mode": "delegated"})
    assert result["headline"] == "1 work item created under KS-12"
    assert result["summary"].startswith("KS-12 stays an Epic")
    assert [c["key"] for c in result["created"]] == ["KS-13"]
    assert recorder.ran("create_jira_issues")


# --- which comments make the ticket the root ----------------------------------------


def test_a_confluence_link_in_the_comment_leaves_the_ticket_alone() -> None:
    from src.context.ingest import has_url, is_pointer_comment

    for comment in ("@Aetherion build", "@Aetherion build based on the description"):
        assert is_pointer_comment(comment) and not has_url(comment)
    link = "@Aetherion build https://x.atlassian.net/wiki/spaces/BGV/pages/1/Spec"
    assert has_url(link)


# --- a comment with its own requirement: link only when related ----------------------


def own_request_breakdown() -> dict:
    return small()


async def create_from_comment(fake, request: str = "Export verification results to Excel."):
    return await create_jira_issues(
        breakdown=own_request_breakdown(),
        requirement=request,
        project_key="BGV",
        idempotency_basis=request,
        source_key="BGV-20",
        requested_by="Laxman, 2026-09-28",
    )


@pytest.fixture
def bgv20(fake_jira, jira_env):
    def install(summary: str, description: str = ""):
        spec = {
            "summary": summary,
            "description": {
                "type": "doc",
                "version": 1,
                "content": (
                    [{"type": "paragraph", "content": [{"type": "text", "text": description}]}]
                    if description
                    else []
                ),
            },
            "issuetype": "Task",
        }
        return fake_jira(issue_types=TYPES, epics={"BGV-20": spec})

    return install


def links_posted(fake) -> list[str]:
    return [url for method, url in fake.writes if url.endswith("/issueLink")]


def stub_model(monkeypatch, related: bool | None) -> None:
    from src.classification import decompose

    async def chat(prompt: str) -> str:
        if related is None:
            raise ConnectionError("no model")
        return json.dumps({"related": related, "reason": "test"})

    monkeypatch.setattr(decompose, "_chat", chat)


async def test_an_unrelated_ticket_gets_no_link_and_the_origin_is_written(bgv20, monkeypatch):
    fake = bgv20("Agent test ticket")
    stub_model(monkeypatch, related=False)
    result = await create_from_comment(fake)
    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert links_posted(fake) == []
    # A Small request is one Task (2026-10-07).
    story = next(i for i in fake.created if i["fields"]["issuetype"]["name"] == "Task")
    assert "Requested in a comment on BGV-20 by Laxman, 2026-09-28." in json.dumps(
        story["fields"]["description"]
    )
    assert fake.epics["BGV-20"]["issuetype"] == "Task", "never converted"


async def test_a_related_ticket_is_linked(bgv20, monkeypatch):
    fake = bgv20("Verification exports", "Recruiters need exports of verification results.")
    stub_model(monkeypatch, related=True)
    await create_from_comment(fake)
    assert len(links_posted(fake)) == 1
    # A Small request is one Task (2026-10-07).
    story = next(i for i in fake.created if i["fields"]["issuetype"]["name"] == "Task")
    assert "Requested in a comment" not in json.dumps(story["fields"]["description"])


@pytest.mark.parametrize(
    "summary,description,linked",
    [
        ("Agent test ticket", "", False),
        ("Verification results export", "", True),
        ("Reports", "HR can export verification results to Excel for audits.", True),
    ],
)
async def test_without_the_model_the_words_decide(bgv20, monkeypatch, summary, description, linked):
    fake = bgv20(summary, description)
    stub_model(monkeypatch, related=None)
    await create_from_comment(fake)
    assert bool(links_posted(fake)) is linked
