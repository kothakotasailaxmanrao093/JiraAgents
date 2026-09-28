"""Flaws found by the 15-scenario live test on BGV (2026-09-24), one test each.

Fixtures use what the live runs actually produced: BGV-20's description and the
titles Jira holds for BGV-21..31, BGV-11's explain request, BGV-16's creation.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from src.agent.delegated import to_contract
from src.classification import decompose
from src.classification.decompose import llm_breakdown, requirement_lines, uncovered_lines
from src.classification.validate import ProjectContext
from src.context.ingest import assemble_requirement
from src.jira.issues import link_related
from src.models.schemas import ResultStatus, SourceIssue, WorkBreakdown
from src.shared.contract import Outcome
from src.tools import tools

BGV20 = (
    "Verify the highest degree a candidate has declared.\n"
    "- Candidate uploads the degree certificate (PDF or image, max 10 MB)\n"
    "- Officer sends a verification request to the university's registrar email\n"
    "- University replies Confirmed or Not found; the officer records the reply and attaches it\n"
    '- If there is no reply in 10 working days, send one reminder; after 15 days mark "Unable to verify"\n'
    '- A "Not found" result raises a red flag on the candidate report and notifies the recruiter\n'
    "- Every action is recorded in the candidate's activity log"
)


def _source(**over: Any) -> SourceIssue:
    base = dict(
        key="BGV-20",
        summary="Education certificate verification",
        description=BGV20,
        trigger_comment_id="1",
        trigger_comment_author="Laxman",
        trigger_comment_body="@Aetherion build",
    )
    base.update(over)
    return SourceIssue(**base)


def _story(title: str, subtasks: list[str]) -> dict[str, Any]:
    return {
        "title": title,
        "user_story_statement": f"As an officer, I want to {title.lower()}, so that checks finish.",
        "description": f"Deliver: {title}.",
        "business_value": "Faster verification.",
        "priority": "Medium",
        "estimated_complexity": "Medium",
        "dependencies": "None",
        "acceptance_criteria": [f"{title} works."],
        "subtasks": [
            {
                "title": t,
                "description": f"Build {t}.",
                "expected_outcome": f"{t} done.",
                "dependencies": "None",
                "completion_criteria": f"{t} verified.",
            }
            for t in subtasks
        ],
    }


# The five Stories BGV-20 actually produced — no Story or Sub-task for the flag.
LIVE_BGV20 = {
    "classification": "Medium",
    "analysis": "Several related capabilities.",
    "epic": {
        "business_objective": "Verify declared degrees, flagging Not found results.",
        "scope": ["Degree verification"],
        "out_of_scope": ["Not specified"],
        "priority": "High",
        "acceptance_criteria": ["A Not found result raises a red flag."],
        "jira_summary": "Implement Candidate Degree Verification Process",
    },
    "stories": [
        _story("Candidate Degree Certificate Upload", ["Develop Upload Functionality"]),
        _story("Officer Sends Verification Request", ["Implement Email Sending Feature"]),
        _story("Handle University Response", ["Develop Response Recording Feature"]),
        _story("Send Reminder and Mark Unable to Verify", ["Implement Reminder System"]),
        _story("Notification and Logging of Actions", ["Develop Notification and Logging"]),
    ],
}
LIVE_COVERAGE = {
    "1": "Candidate Degree Certificate Upload",
    "2": "Officer Sends Verification Request",
    "3": "Handle University Response",
    "4": "Send Reminder and Mark Unable to Verify",
    "5": "Implement Candidate Degree Verification Process",  # the Epic — does not count
    "6": "Notification and Logging of Actions",
}


# --- flaw 1: a requirement line silently dropped --------------------------


def test_the_stated_requirement_lines_are_found_in_the_assembled_text() -> None:
    text, _ = assemble_requirement(_source())
    lines = requirement_lines(text)
    assert len(lines) == 6
    assert "red flag" in lines[4]


def test_a_line_only_the_epic_mentions_is_uncovered() -> None:
    text, _ = assemble_requirement(_source())
    lines = requirement_lines(text)
    breakdown = WorkBreakdown.model_validate(json.loads(json.dumps(LIVE_BGV20)))
    assert uncovered_lines(lines, LIVE_COVERAGE, breakdown) == [5]


async def test_an_uncovered_line_is_named_in_one_retry(monkeypatch) -> None:
    text, _ = assemble_requirement(_source())
    first = {**LIVE_BGV20, "coverage": LIVE_COVERAGE}
    fixed = json.loads(json.dumps(LIVE_BGV20))
    fixed["stories"].append(_story("Flag Not Found Results", ["Show a red flag on the report"]))
    fixed["coverage"] = {**LIVE_COVERAGE, "5": "Flag Not Found Results"}
    prompts: list[str] = []

    async def _chat(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(first if len(prompts) == 1 else fixed)

    monkeypatch.setattr(decompose, "_chat", _chat)
    breakdown = await llm_breakdown(text, ProjectContext())
    # A third, small request may follow to sharpen vague completion criteria.
    assert len(prompts) >= 2
    assert "REQUIREMENT LINES" in prompts[0]
    assert "red flag" in prompts[1].split("Your previous answer was rejected:")[1]
    assert "Flag Not Found Results" in [s.title for s in breakdown.stories]


async def test_a_line_still_uncovered_after_the_retry_stops_the_run(monkeypatch) -> None:
    text, _ = assemble_requirement(_source())

    async def _chat(prompt: str) -> str:
        return json.dumps({**LIVE_BGV20, "coverage": LIVE_COVERAGE})

    monkeypatch.setattr(decompose, "_chat", _chat)
    with pytest.raises(decompose.DecompositionError, match="red flag"):
        await llm_breakdown(text, ProjectContext())


def test_a_single_sentence_has_no_lines_to_hold_the_model_to() -> None:
    assert requirement_lines("Allow users to update their preferred language.") == []


# --- flaw 3: the reply carried the internal status block ------------------


def _created_run() -> dict[str, Any]:
    return {
        "status": ResultStatus.JIRA_CREATED.value,
        "message": "1 Story, and 3 Subtasks created successfully.",
        "summary_text": "Outcome        : Done - tickets are in Jira\nStatus         : JIRA_CREATED",
        "jira_result": {
            "status": ResultStatus.JIRA_CREATED.value,
            "epic": None,
            "stories": [{"key": "BGV-16", "issue_type": "Story", "summary": "Enable PDF Download"}],
            "subtasks": [
                {"key": "BGV-17", "issue_type": "Sub-task", "summary": "Design PDF Layout"}
            ],
            "created_keys": ["BGV-16", "BGV-17"],
        },
        "source_issue": {"key": "BGV-15"},
    }


def test_the_reply_gets_the_plain_message_not_the_status_block() -> None:
    result = to_contract(_created_run(), run_id="r1", agent_version="t")
    assert result.summary == "1 Story, and 3 Subtasks created successfully."
    assert "JIRA_CREATED" not in result.summary


def test_created_tickets_are_listed_with_their_titles() -> None:
    result = to_contract(_created_run(), run_id="r1", agent_version="t")
    assert [(c.key, c.summary) for c in result.created] == [
        ("BGV-16", "Enable PDF Download"),
        ("BGV-17", "Design PDF Layout"),
    ]


def test_the_ticket_sub_tasks_were_attached_to_is_not_listed_as_created() -> None:
    run = _created_run()
    run["jira_result"]["stories"] = [{"key": "BGV-3", "issue_type": "Story", "summary": "x"}]
    run["jira_result"]["created_keys"] = ["BGV-17"]
    result = to_contract(run, run_id="r1", agent_version="t")
    assert [c.key for c in result.created] == ["BGV-17"]


# --- flaw 4: explain -------------------------------------------------------


def test_an_answer_is_its_own_outcome_headed_with_the_key() -> None:
    run = {
        "status": ResultStatus.EXPLAINED.value,
        "message": "This story checks a candidate's two most recent employers.",
        "source_issue": {"key": "BGV-11"},
    }
    result = to_contract(run, run_id="r1", agent_version="t")
    assert result.outcome is Outcome.ANSWERED
    assert result.headline == "About BGV-11"
    assert result.summary.startswith("This story checks")


async def test_the_model_writes_the_explanation(monkeypatch) -> None:
    async def _chat(prompt: str) -> str:
        assert "explain this" in prompt and "two employers" in prompt
        return json.dumps({"answer": "It checks the candidate's last two jobs."})

    monkeypatch.setattr(decompose, "_chat", _chat)
    answer = await decompose.explain_issue("explain this", "Verify the last two employers.")
    assert answer == "It checks the candidate's last two jobs."


async def test_no_model_means_the_ticket_is_quoted_rather_than_nothing() -> None:
    # conftest's _no_gateway makes every model call fail.
    with pytest.raises(Exception):
        await decompose.explain_issue("explain this", "text")


# --- flaw 5: generic questions --------------------------------------------


async def test_a_thin_request_gets_questions_about_itself(monkeypatch) -> None:
    async def _chat(prompt: str) -> str:
        return json.dumps(
            {
                "verdict": "CLARIFICATION_REQUIRED",
                "reason": "Which dashboard is unknown.",
                "questions": ["Which dashboard — recruiter, candidate or client?"],
            }
        )

    monkeypatch.setattr(decompose, "_chat", _chat)
    out = await tools.validate_requirement("build make the dashboard better", None)
    assert out["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert out["clarifying_questions"] == ["Which dashboard — recruiter, candidate or client?"]


async def test_without_the_model_the_fixed_questions_are_still_asked() -> None:
    out = await tools.validate_requirement("build make the dashboard better", None)
    assert out["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert out["clarifying_questions"]


# --- flaw 6: new work not linked back ---------------------------------------


async def test_new_work_is_linked_to_the_ticket_it_was_asked_on() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx.Response(201)

    async with httpx.AsyncClient(
        base_url="https://x.atlassian.net", transport=httpx.MockTransport(handler)
    ) as client:
        await link_related(client, "BGV-15", "BGV-16")
    assert sent == [
        {
            "path": "/rest/api/3/issueLink",
            "body": {
                "type": {"name": "Relates"},
                "inwardIssue": {"key": "BGV-15"},
                "outwardIssue": {"key": "BGV-16"},
            },
        }
    ]


# --- BGV-46: an identical earlier request, reused -----------------------------


def test_a_reused_build_says_it_already_exists_and_names_the_keys() -> None:
    run = {
        "status": ResultStatus.JIRA_CREATED.value,
        "message": "Existing issues for this requirement were found and reused.",
        "jira_result": {
            "status": ResultStatus.JIRA_CREATED.value,
            "reused_existing": True,
            "epic": {"key": "BGV-34", "issue_type": "Epic", "summary": "Address History"},
            "stories": [{"key": "BGV-35", "issue_type": "Story", "summary": "Capture history"}],
            "subtasks": [],
            "created_keys": [],
            "reused_keys": ["BGV-34", "BGV-35"],
        },
        "source_issue": {"key": "BGV-46"},
    }
    result = to_contract(run, run_id="r1", agent_version="t")
    assert result.outcome is Outcome.ALREADY_EXISTS
    assert result.headline == "This work already exists"
    assert result.created == []
    assert [(d.existing_key, d.existing_summary) for d in result.duplicates] == [
        ("BGV-34", "Address History"),
        ("BGV-35", "Capture history"),
    ]


# --- BGV-33: one Sub-task per Story ---------------------------------------------


async def test_a_story_with_one_sub_task_is_asked_to_split_once(monkeypatch) -> None:
    thin = json.loads(json.dumps(LIVE_BGV20))
    deep = json.loads(json.dumps(LIVE_BGV20))
    for story in deep["stories"]:
        story["subtasks"] = story["subtasks"] * 2
        story["subtasks"][1] = {
            **story["subtasks"][1],
            "title": story["subtasks"][1]["title"] + " II",
        }
    prompts: list[str] = []

    async def _chat(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(thin if len(prompts) == 1 else deep)

    monkeypatch.setattr(decompose, "_chat", _chat)
    breakdown = await llm_breakdown("Verify a degree.", ProjectContext())
    retry = next(p for p in prompts if "Your previous answer was rejected" in p)
    assert "only one Sub-task" in retry
    assert all(len(s.subtasks) == 2 for s in breakdown.stories)


async def test_a_still_thin_answer_is_accepted_rather_than_failing(monkeypatch) -> None:
    async def _chat(prompt: str) -> str:
        return json.dumps(LIVE_BGV20)

    monkeypatch.setattr(decompose, "_chat", _chat)
    breakdown = await llm_breakdown("Verify a degree.", ProjectContext())
    assert len(breakdown.stories) == 5


# --- BGV-57: "Officer ID Verification" was "covered" by an address ticket ------

BGV46 = (
    "Candidates must provide their address history for the last 5 years.\n"
    "- Candidate enters each address with from and to dates\n"
    "- Gaps longer than 30 days must be explained in a note\n"
    "- Officer can mark each address as Verified or Not verified\n"
    "- The report shows the full address history with verification status"
)


def test_two_common_words_in_a_long_description_are_not_a_duplicate() -> None:
    from src.jira.duplicates import find_overlaps
    from src.models.schemas import ExistingIssue

    existing = [
        ExistingIssue(
            key="BGV-46",
            summary="Candidate address history",
            issue_type="Story",
            description=BGV46,
        )
    ]
    assert find_overlaps(["Officer ID Verification"], existing) == []


def test_a_long_title_fully_described_elsewhere_still_matches_by_words() -> None:
    from src.jira.duplicates import find_overlaps
    from src.models.schemas import ExistingIssue

    existing = [
        ExistingIssue(
            key="BGV-46",
            summary="Candidate address history",
            issue_type="Story",
            description=BGV46,
        )
    ]
    matches = find_overlaps(["Note for address history gaps"], existing)
    assert [m.existing_key for m in matches] == ["BGV-46"]
    assert matches[0].matched_on == "description"


# --- BGV-76: agent-made tickets failed the agent's own review -------------------


def test_open_questions_are_written_to_the_story_in_jira() -> None:
    from src.jira.adf import story_description
    from src.models.schemas import Story

    story = Story.model_validate(
        {
            **_story("Handle Unpaid Invoices", ["Find invoices unpaid over 30 days", "Email"]),
            "open_questions": ["How many reminders are sent, and how far apart?"],
        }
    )
    doc = story_description(story)
    headings = [b["content"][0]["text"] for b in doc["content"] if b["type"] == "heading"]
    assert headings[-1] == "Open questions — not stated in the requirement"
    assert "How many reminders" in json.dumps(doc)


def test_the_prompt_asks_for_testable_criteria_and_self_contained_sub_tasks() -> None:
    from src.prompts.templates import SYSTEM_PROMPT

    assert "Given <situation>, when" in SYSTEM_PROMPT
    assert "OPEN QUESTIONS" in SYSTEM_PROMPT
    assert 'never "works"' in SYSTEM_PROMPT


# --- BGV-79..88: prompt rules the model skipped --------------------------------


def _bgv86(**over: Any) -> dict[str, Any]:
    story = _story("Notify Recruiter on Case Completion", ["Setup Email", "Trigger Email"])
    story["subtasks"][1]["completion_criteria"] = "Emails are sent as per the completion criteria."
    story.update(over)
    return {"classification": "Small", "analysis": "One capability.", "stories": [story]}


def test_bgv88s_completion_criterion_is_named() -> None:
    """Open questions are no longer forced (D1 Test A: a complete requirement
    must produce none); the uncheckable criterion still is."""
    from src.classification.decompose import _quality_problems

    problems = " ".join(_quality_problems(WorkBreakdown.model_validate(_bgv86())))
    assert "open_questions" not in problems
    assert '"Trigger Email"' in problems


def test_a_checkable_criterion_naming_the_completed_status_is_not_vague() -> None:
    from src.classification.decompose import _quality_problems

    data = _bgv86(
        open_questions=[
            "Which recruiter is emailed when several own a case?",
            "Is the email sent again if a case is reopened and completed twice?",
            "What happens if the email cannot be delivered?",
        ],
        acceptance_criteria=[
            "Given a case, when it is marked Completed, then its recruiter is emailed within 1 hour",
            "Given a case still In Progress, when the hourly check runs, then no email is sent",
        ],
    )
    for t in data["stories"][0]["subtasks"]:
        t["completion_criteria"] = (
            "A case marked Completed at 10:00 produces an email to its recruiter by 11:00."
        )
    assert _quality_problems(WorkBreakdown.model_validate(data)) == []


# --- BGV-92..102: "implemented and tested" survived two asks --------------------


async def test_vague_completion_criteria_are_rewritten_by_one_small_request(monkeypatch) -> None:
    from src.classification.decompose import _sharpen_criteria

    data = _bgv86()
    data["stories"][0]["subtasks"][0][
        "completion_criteria"
    ] = "Email setup is implemented and tested."
    breakdown = WorkBreakdown.model_validate(data)

    async def _chat(prompt: str) -> str:
        assert "Rewrite ONLY the completion criteria" in prompt
        return json.dumps(
            {
                "criteria": {
                    "Setup Email": "A test message sent from the reminder sender reaches the recruiter inbox.",
                    "Trigger Email": "A case marked Completed at 10:00 produces an email to its recruiter by 11:00.",
                }
            }
        )

    monkeypatch.setattr(decompose, "_chat", _chat)
    sharpened = await _sharpen_criteria(breakdown, "Email the recruiter within 1 hour.")
    criteria = [t.completion_criteria for t in sharpened.stories[0].subtasks]
    assert criteria[1].startswith("A case marked Completed at 10:00")
    assert "implemented" not in " ".join(criteria)


async def test_a_vague_rewrite_is_refused_and_a_failed_request_keeps_the_breakdown(
    monkeypatch,
) -> None:
    from src.classification.decompose import _sharpen_criteria

    breakdown = WorkBreakdown.model_validate(_bgv86())

    async def _chat(prompt: str) -> str:
        return json.dumps({"criteria": {"Trigger Email": "Emails are sent successfully."}})

    monkeypatch.setattr(decompose, "_chat", _chat)
    kept = await _sharpen_criteria(breakdown, "x")
    assert kept.stories[0].subtasks[1].completion_criteria.startswith("Emails are sent as per")


# --- BGV-40: out-of-scope items were demanded as work ---------------------------

# The stated block exactly as assembly produced it for BGV-40 (2026-09-25):
# section headings glued onto the end of the previous bullet.
BGV40_STATED = (
    "## Stated requirement (the description of BGV-40)\n"
    "Goal: bill each client every month. Who uses it:\n"
    "- Billing officer: reviews and sends invoices\n"
    "- Account manager: follows up unpaid invoices\n"
    "Unpaid invoices:\n"
    "- An invoice not paid by its due date becomes Overdue.\n"
    "- When a billing officer marks an invoice Paid, no further reminders are sent. Out of scope:\n"
    "- Online card payment\n"
    "- Currencies other than INR\n"
    "- Credit notes and refunds\n"
)


def test_out_of_scope_items_are_never_demanded_as_work() -> None:
    lines = requirement_lines(BGV40_STATED)
    assert "Online card payment" not in lines
    assert "Credit notes and refunds" not in lines
    assert (
        lines[-1] == "When a billing officer marks an invoice Paid, no further reminders are sent."
    )


def test_a_heading_on_its_own_line_also_starts_an_excluded_section() -> None:
    text = "- Send the invoice\n- Show the invoice\nNot in scope:\n- Refunds\n- Card payments\n"
    assert requirement_lines(text) == ["Send the invoice", "Show the invoice"]


def test_a_section_after_the_exclusions_is_covered_again() -> None:
    text = "- Send the invoice\nExcluded:\n- Refunds\nReminders:\n- Remind after 30 days\n"
    assert requirement_lines(text) == ["Send the invoice", "Remind after 30 days"]


def test_a_story_is_not_matched_against_an_epics_broad_description() -> None:
    """BGV-41: "Generate Monthly Invoices" matched Epic BGV-26 at 1.00."""
    from src.jira.duplicates import find_overlaps
    from src.models.schemas import ExistingIssue

    epic = ExistingIssue(
        key="BGV-26",
        summary="Automate Client Billing and Invoicing",
        issue_type="Epic",
        description="Generate monthly invoices for completed checks, email invoice PDFs, "
        "show past invoices in the portal and remind clients about unpaid invoices.",
    )
    assert find_overlaps(["Generate Monthly Invoices"], [epic]) == []


# --- Team-managed BGV: "Subtask", not "Sub-task" (2026-09-27) ------------------------


def test_a_team_managed_projects_subtask_spelling_is_used() -> None:
    from src.jira.client import resolve_issue_types

    types = resolve_issue_types(["Subtask", "Epic", "Story", "Bug", "Task"])
    assert types == {"epic": "Epic", "story": "Story", "subtask": "Subtask"}


def test_a_company_managed_project_keeps_sub_task() -> None:
    from src.jira.client import resolve_issue_types

    assert resolve_issue_types(["Sub-task", "Epic", "Story"])["subtask"] == "Sub-task"


def test_a_truly_missing_type_is_still_reported() -> None:
    from src.jira.client import missing_issue_types, resolve_issue_types

    types = resolve_issue_types(["Epic", "Story", "Bug"])
    assert missing_issue_types([types["story"], types["subtask"]], ["Epic", "Story", "Bug"]) == [
        "Sub-task"
    ]


def test_a_custom_configured_name_never_falls_back_to_a_default() -> None:
    import os

    from src.jira.client import resolve_issue_types

    os.environ["JIRA_EPIC_ISSUE_TYPE"] = "Initiative"
    try:
        assert resolve_issue_types(["Epic", "Story", "Subtask"])["epic"] == "Initiative"
    finally:
        del os.environ["JIRA_EPIC_ISSUE_TYPE"]
