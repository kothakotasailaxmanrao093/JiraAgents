"""The webhook flow end to end, with every tool mocked.

Asserts the decisions the agent makes: when it emails, what kind, whether it
writes back, which marker label it sets, and above all that duplicates never
reach ``create_jira_issues``.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agent import agent as agent_module
from src.agent.agent import JiraTaskCreation
from src.models.schemas import ResultStatus
from src.shared.contract import AgentResult


def source_issue(**overrides: Any) -> dict[str, Any]:
    base = {
        "ok": True,
        "key": "KS-12",
        "project_key": "KS",
        "project_name": "Knowledge Suite",
        "summary": "Aetherion: notifications",
        "requirement_text": "## Stated requirement\nSend email and SMS notifications.",
        "trigger_matched": True,
        "already_processed": False,
        "notes": [],
        "requested_placement": "backlog",
        "trigger_comment_id": "9001",
        "trigger_comment_author": "Priya",
        "trigger_comment_body": "@Aetherion Send email and SMS notifications.",
    }
    base.update(overrides)
    return base


def jira_context() -> dict[str, Any]:
    return {
        "ok": True,
        "project": {"key": "KS", "name": "Knowledge Suite"},
        "existing_issues": [],
        "epic": None,
        "sprint": None,
        "notes": [],
    }


def breakdown(classification: str = "Medium") -> dict[str, Any]:
    story = {
        "title": "Receive email notifications",
        "user_story_statement": (
            "As a registered user, I want to receive email notifications, "
            "so that I hear about changes that affect me."
        ),
        "description": "Users are emailed when a watched item changes.",
        "business_value": "Keeps users informed without them polling the product.",
        "priority": "High",
        "estimated_complexity": "Medium",
        "dependencies": "None",
        "acceptance_criteria": ["An enabled user receives an email within the agreed window."],
        "subtasks": [
            {
                "title": "Define the notification rules",
                "description": "Agree which events raise a notification.",
                "expected_outcome": "An approved list of notifiable events.",
                "dependencies": "None",
                "completion_criteria": "The list is signed off by the product owner.",
            }
        ],
    }
    return {
        "classification": classification,
        "analysis": "Several related delivery capabilities.",
        "epic": {
            "business_objective": "Let users receive and control notifications.",
            "scope": ["Email delivery", "SMS delivery"],
            "out_of_scope": ["Not specified"],
            "priority": "High",
            "acceptance_criteria": ["A user receives a notification on every enabled channel."],
            "jira_summary": "Notification delivery and preferences",
        },
        "stories": [story, {**story, "title": "Receive SMS notifications"}],
    }


class Recorder:
    """Records every tool call and replays a queued result per tool name."""

    def __init__(self, results: dict[str, Any]) -> None:
        self.results = results
        self.calls: list[tuple[str, tuple, dict]] = []

    async def execute(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        result = self.results.get(name)
        if callable(result):
            return result(*args)
        return result if result is not None else {}

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def call(self, name: str) -> tuple[str, tuple, dict] | None:
        for entry in self.calls:
            if entry[0] == name:
                return entry
        return None

    def ran(self, name: str) -> bool:
        return name in self.names()

    # report_to_issue is invoked positionally, so adding one parameter to it
    # used to shift every index the tests below assert on — nine assertions
    # broke over a single new argument. Naming the positions once means the
    # next person to add one updates this tuple and nothing else.
    _REPORT_FIELDS = (
        "issue_key",
        "headline",
        "situation",
        "created",
        "questions",
        "duplicates",
        "errors",
        "context_notes",
        "conflicts",
        "notified",
        "mark_processed",
        "mark_awaiting",
        "answered_comment_id",
        "quality_warning",
    )

    def report(self) -> dict[str, Any]:
        """The report_to_issue arguments, by name."""
        entry = self.call("report_to_issue")
        assert entry is not None, "report_to_issue was never called"
        return dict(zip(self._REPORT_FIELDS, entry[1], strict=False))


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch):
    def install(**results: Any) -> Recorder:
        recorder = Recorder(results)
        monkeypatch.setattr(agent_module.toolExecutor, "execute", recorder.execute)
        return recorder

    return install


DEFAULTS = {
    "notify_email": {"attempted": True, "sent": True, "recipients": ["lead@example.com"]},
    "report_to_issue": {"ok": True, "comment_id": "10001", "labels_set": []},
}


# --- silent stops -----------------------------------------------------------


async def test_an_issue_without_the_keyword_stops_silently(tools) -> None:
    recorder = tools(read_jira_issue=source_issue(trigger_matched=False), **DEFAULTS)

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert result["skipped_reason"] == "trigger_keyword_absent"
    assert not recorder.ran("notify_email"), "a non-matching ticket must not email anyone"
    assert not recorder.ran("report_to_issue")
    assert not recorder.ran("create_jira_issues")


async def test_an_already_processed_issue_stops_silently(tools) -> None:
    recorder = tools(read_jira_issue=source_issue(already_processed=True), **DEFAULTS)

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert result["skipped_reason"] == "already_processed"
    assert not recorder.ran("notify_email")
    assert not recorder.ran("create_jira_issues")


async def test_force_overrides_both_gates(tools) -> None:
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["KS-30"],
            "summary": "Created.",
        },
        **DEFAULTS,
    )

    await JiraTaskCreation.fn({"issue_key": "KS-12", "force": True})

    _, args, _ = recorder.call("read_jira_issue")
    assert args[1] is False, "force must disable the keyword requirement"
    assert args[2] is False, "force must disable the processed-label skip"


# --- clarification ----------------------------------------------------------


async def test_clarification_emails_the_situation_and_marks_awaiting(tools) -> None:
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={
            "status": ResultStatus.CLARIFICATION_REQUIRED.value,
            "reason": "The approving role is unknown.",
            "clarifying_questions": ["Who approves?", "What happens after rejection?"],
        },
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert not recorder.ran("create_jira_issues"), "nothing may be created"

    _, args, _ = recorder.call("notify_email")
    assert args[0] == "clarification_required"
    assert args[1] == "KS-12"
    assert args[4] == "The approving role is unknown."
    assert args[5] == ["Who approves?", "What happens after rejection?"]

    report = recorder.report()
    assert report["mark_processed"] is False, "must not be marked processed"
    assert report["mark_awaiting"] is True, "must be marked awaiting input"


# --- duplicates -------------------------------------------------------------


async def test_duplicates_are_never_created_only_notified(tools) -> None:
    duplicates = [
        {
            "proposed_title": "Receive email notifications",
            "existing_key": "KS-7",
            "existing_summary": "Email alerts for watched items",
            "score": 0.81,
        }
    ]
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={
            "breakdown": breakdown(),
            "counts": {"epics": 1, "stories": 2, "subtasks": 2},
            "duplicate_matches": duplicates,
        },
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert not recorder.ran("create_jira_issues"), "duplicates must never be created"
    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert result["duplicate_matches"] == duplicates
    assert "KS-7" in result["message"]

    _, args, _ = recorder.call("notify_email")
    assert args[0] == "duplicates_found"
    assert args[6] == duplicates

    report = recorder.report()
    assert report["mark_processed"] is False
    assert report["mark_awaiting"] is True, "a duplicate stop waits for a human decision"


async def test_duplicates_stop_the_run_in_manual_mode_too(tools) -> None:
    recorder = tools(
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={
            "breakdown": breakdown(),
            "counts": {},
            "duplicate_matches": [
                {
                    "proposed_title": "Receive email notifications",
                    "existing_key": "KS-7",
                    "existing_summary": "Email alerts",
                    "score": 0.9,
                }
            ],
        },
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn(
        {"requirement": "Send email and SMS notifications.", "project_key": "KS"}
    )

    assert not recorder.ran("create_jira_issues")
    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert recorder.ran("notify_email"), "manual runs still notify"
    assert not recorder.ran("report_to_issue"), "manual runs have no ticket to write back to"


# --- failures ---------------------------------------------------------------


async def test_an_unreadable_trigger_issue_emails_the_failure(tools) -> None:
    recorder = tools(
        read_jira_issue={"ok": False, "error": "Issue 'KS-99' was not found.", "key": "KS-99"},
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-99"})

    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    _, args, _ = recorder.call("notify_email")
    assert args[0] == "failed"
    assert "was not found" in args[4]
    assert not recorder.ran("report_to_issue"), "an unreadable issue cannot be commented on"


async def test_a_bad_jira_context_emails_and_writes_back(tools) -> None:
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context={"ok": False, "error": "Project 'KS' has no Sub-task type."},
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert recorder.call("notify_email")[1][0] == "failed"
    assert recorder.ran("report_to_issue")
    assert not recorder.ran("create_jira_issues")


async def test_a_decomposition_failure_emails_the_reason(tools) -> None:
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={"breakdown": None, "error": "model returned invalid JSON"},
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    _, args, _ = recorder.call("notify_email")
    assert args[0] == "failed"
    assert args[8] == ["model returned invalid JSON"]


async def test_a_partial_jira_failure_reports_what_was_created(tools) -> None:
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATION_FAILED.value,
            "created_keys": ["KS-30"],
            "failure_reason": "Sub-task rejected by Jira.",
            "stories": [],
            "subtasks": [],
        },
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    _, args, _ = recorder.call("notify_email")
    assert args[0] == "failed"
    assert args[7] == ["KS-30"], "the partial creations must reach the email"
    assert "Already created: KS-30" in result["message"]


# --- success ----------------------------------------------------------------


async def test_a_successful_run_writes_back_and_marks_processed(tools) -> None:
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={
            "breakdown": breakdown(),
            "counts": {"epics": 1, "stories": 2, "subtasks": 2},
            "duplicate_matches": [],
        },
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["KS-30", "KS-31"],
            "epic": {"key": "KS-30", "issue_type": "Epic", "summary": "Notifications"},
            "stories": [{"key": "KS-31", "issue_type": "Story", "summary": "Email"}],
            "subtasks": [],
            "summary": "Created 1 Epic and 1 Story.",
        },
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert result["status"] == ResultStatus.JIRA_CREATED.value
    report = recorder.report()
    assert report["mark_processed"] is True, "a completed run marks the issue processed"
    assert report["mark_awaiting"] is False
    created = report["created"]
    assert [ref["key"] for ref in created] == ["KS-30", "KS-31"]


async def test_the_idempotency_key_is_not_derived_from_the_comment_id(tools) -> None:
    """The same request asked twice must resolve to the same tickets.

    The key used to default to "<issue>#<comment>". Because it already hashes
    the request text, two different requests on one ticket differ anyway — but
    the SAME request asked again in a second comment got a different label,
    found nothing to reuse, and created the whole hierarchy a second time.
    Which comment was answered is tracked separately, by the answered-comments
    issue property, so nothing needs the comment id here.
    """
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={
            "status": ResultStatus.READY_FOR_JIRA.value,
            "requirement": "x",
            "request_text": "Let drivers log a rest break",
        },
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["KS-30"],
            "summary": "ok",
        },
        **DEFAULTS,
    )

    await JiraTaskCreation.fn({"issue_key": "KS-12"})

    _, args, _ = recorder.call("create_jira_issues")
    assert args[3] == "", "no correlation id is invented from the comment"


async def test_an_explicit_correlation_id_is_still_honoured(tools) -> None:
    """A caller with its own request identity keeps control of the key."""
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["KS-30"],
            "summary": "ok",
        },
        **DEFAULTS,
    )

    await JiraTaskCreation.fn({"issue_key": "KS-12", "correlation_id": "batch-7"})

    _, args, _ = recorder.call("create_jira_issues")
    assert args[3] == "batch-7"


async def test_the_stated_request_is_what_keys_the_run(tools) -> None:
    """Not the assembled bundle, which grows with every new comment."""
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={
            "status": ResultStatus.READY_FOR_JIRA.value,
            "requirement": "the whole bundle, ticket + comments + history",
            "request_text": "Let drivers log a rest break",
        },
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["KS-30"],
            "summary": "ok",
        },
        **DEFAULTS,
    )

    await JiraTaskCreation.fn({"issue_key": "KS-12"})

    _, args, _ = recorder.call("create_jira_issues")
    assert args[8] == "Let drivers log a rest break"


async def test_create_in_jira_false_returns_the_proposal_without_writing(tools) -> None:
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={
            "breakdown": breakdown(),
            "counts": {"epics": 1, "stories": 2, "subtasks": 2},
            "duplicate_matches": [],
        },
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-12", "create_in_jira": False})

    assert result["status"] == ResultStatus.READY_FOR_JIRA.value
    assert not recorder.ran("create_jira_issues")
    assert not recorder.ran("notify_email")


# --- manual mode ------------------------------------------------------------


async def test_manual_mode_never_reads_or_writes_a_trigger_issue(tools) -> None:
    recorder = tools(
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["KS-30"],
            "summary": "ok",
        },
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn(
        {"requirement": "Send email and SMS notifications.", "project_key": "KS"}
    )

    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert not recorder.ran("read_jira_issue")
    assert not recorder.ran("report_to_issue")
    assert result["source_issue"] is None


async def test_an_undelivered_email_does_not_fail_the_run(tools) -> None:
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={
            "status": ResultStatus.CLARIFICATION_REQUIRED.value,
            "reason": "unclear",
            "clarifying_questions": ["Who approves?"],
        },
        notify_email={"attempted": True, "sent": False, "error": "SMTP timeout"},
        report_to_issue={"ok": True, "comment_id": "1", "labels_set": []},
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert result["notification"]["sent"] is False
    report = recorder.report()
    assert "SMTP timeout" in report["notified"], "the ticket records that the email failed"


# --- partial vs full overlap ------------------------------------------------
# Wholly-existing work and partly-existing work need different answers from a
# human, so they get different emails.


def overlap_result(covered: list[str], remaining: list[str]) -> dict[str, Any]:
    matches = [
        {
            "proposed_title": title,
            "existing_key": f"ORD-{n}",
            "existing_summary": "Account access",
            "existing_status": "In Progress",
            "existing_type": "Story",
            "existing_url": f"https://x.atlassian.net/browse/ORD-{n}",
            "score": 1.0,
            "matched_on": "description",
        }
        for n, title in enumerate(covered, start=90)
    ]
    return {
        "breakdown": breakdown(),
        "counts": {"epics": 1, "stories": 2, "subtasks": 2},
        "duplicate_matches": matches,
        "overlap": {
            "matches": matches,
            "covered_titles": covered,
            "remaining_titles": remaining,
        },
        "overlap_is_partial": bool(covered and remaining),
        "overlap_is_full": bool(covered and not remaining),
    }


async def test_fully_existing_work_sends_the_duplicates_email(tools) -> None:
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown=overlap_result(["Create logout"], []),
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "ORD-20"})

    assert not recorder.ran("create_jira_issues")
    assert recorder.call("notify_email")[1][0] == "duplicates_found"
    assert "already exists" in result["message"]


async def test_partly_existing_work_sends_the_partial_email_with_the_remainder(tools) -> None:
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown=overlap_result(
            ["Create logout"], ["Create two factor authentication"]
        ),
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "ORD-21"})

    assert not recorder.ran("create_jira_issues"), "a partial overlap still creates nothing"
    _, args, _ = recorder.call("notify_email")
    assert args[0] == "partial_duplicate"
    assert args[10] == ["Create two factor authentication"], "the remainder must reach the email"
    assert "Still uncovered" in result["message"]
    assert any("missing part" in q for q in result["clarifying_questions"])


# --- placement taken from the request ---------------------------------------


async def test_the_requested_sprint_placement_is_honoured(tools) -> None:
    recorder = tools(
        read_jira_issue=source_issue(requested_placement="current_sprint"),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["ORD-30"],
            "summary": "ok",
        },
        **DEFAULTS,
    )

    await JiraTaskCreation.fn({"issue_key": "ORD-25"})

    # Gate 1 must be told, because it is what resolves the sprint.
    _, args, _ = recorder.call("inspect_jira_context")
    assert args[2] == "current_sprint"


async def test_backlog_is_used_when_the_request_says_nothing(tools) -> None:
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["ORD-30"],
            "summary": "ok",
        },
        **DEFAULTS,
    )

    await JiraTaskCreation.fn({"issue_key": "ORD-26"})
    assert recorder.call("inspect_jira_context")[1][2] == "backlog"


async def test_an_unidentifiable_sprint_asks_instead_of_failing(tools) -> None:
    """Jira cannot say which sprint — that is a question, not a fault."""
    recorder = tools(
        read_jira_issue=source_issue(requested_placement="current_sprint"),
        inspect_jira_context={
            "ok": False,
            "error": "Current sprint was requested but is unavailable: board 1 has 2 active sprints.",
            "needs_clarification": True,
        },
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "ORD-27"})

    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert not recorder.ran("create_jira_issues")
    _, args, _ = recorder.call("notify_email")
    assert args[0] == "clarification_required"
    assert any("Which sprint" in q for q in args[5])
    report = recorder.report()
    assert report["mark_awaiting"] is True, "the ticket waits for an answer"


# --- questions are answered, never decomposed -------------------------------
# Live defect: "@Aetherion please explain me this ticket properly" created
# Sub-tasks on a real ticket. A question must write a comment and nothing else.


async def test_a_question_is_answered_and_creates_nothing(tools) -> None:
    recorder = tools(
        # trigger_is_question and explanation are decided by read_jira_issue,
        # not by the agent: working them out needs the trigger keyword, which
        # comes from the environment, and a Temporal workflow may not read it.
        read_jira_issue=source_issue(
            issue_type="Task",
            status="To Do",
            reporter="Laxman",
            description="Allow drivers to report a vehicle fault from their phone.",
            trigger_comment_body="@Aetherion please explain me this ticket properly",
            trigger_is_question=True,
            explanation=(
                "KS-12 is a Task, currently To Do, raised by Laxman.\n\n"
                "What it asks for, in its own words:\n\n"
                "Allow drivers to report a vehicle fault from their phone.\n\n"
                "Nothing was created — this was a question, not a request for work."
            ),
        ),
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert result["status"] == ResultStatus.EXPLAINED.value
    # The whole downstream pipeline must be skipped, not merely made harmless.
    for tool in (
        "inspect_jira_context",
        "validate_requirement",
        "generate_work_breakdown",
        "create_jira_issues",
    ):
        assert not recorder.ran(tool), f"{tool} must not run for a question"

    report = recorder.report()
    assert "KS-12" in report["headline"]
    assert "Allow drivers to report a vehicle fault from their phone." in report["situation"]
    assert "Nothing was created" in report["situation"]
    # Answered so it is not replayed, but the ticket is not marked done.
    assert report["answered_comment_id"] == "9001"
    assert report["mark_processed"] is False
    assert report["mark_awaiting"] is False


async def test_a_work_request_still_goes_through_the_full_pipeline(tools) -> None:
    """Counterweight: narrowing must not divert real requirements into prose."""
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={
            "status": ResultStatus.READY_FOR_JIRA.value,
            "requirement": "Send email and SMS notifications.",
        },
        generate_work_breakdown={
            "breakdown": {},
            "classification": "Small",
            "duplicate_matches": [],
            "error": "stop here",
        },
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert result["status"] != ResultStatus.EXPLAINED.value
    assert recorder.ran("validate_requirement")


# --- the invariant every outcome has to satisfy -----------------------------
# Nine separate `_report(...)` call sites each spell out their own headline and
# marker label. Adding a tenth outcome, or a tenth argument, has silently left
# a path unmarked before — the breakdown-failure branch set no label at all, so
# a ticket the agent had given up on looked identical to one nobody had asked
# about. These tests fix the rule rather than the nine call sites.

_STOPS_NEEDING_A_PERSON = [
    pytest.param(
        {
            "read_jira_issue": source_issue(),
            "inspect_jira_context": {
                "ok": False,
                "error": "Current sprint was requested but is unavailable",
                "needs_clarification": True,
            },
        },
        id="ambiguous-sprint",
    ),
    pytest.param(
        {
            "read_jira_issue": source_issue(),
            "inspect_jira_context": {"ok": False, "error": "Jira authentication failed"},
        },
        id="jira-unusable",
    ),
    pytest.param(
        {
            "read_jira_issue": source_issue(),
            "inspect_jira_context": jira_context(),
            "validate_requirement": {
                "status": ResultStatus.CLARIFICATION_REQUIRED.value,
                "bucket": "INCOMPLETE",
                "reason": "Too thin to break down.",
                "clarifying_questions": ["Who is it for?"],
            },
        },
        id="needs-more-detail",
    ),
    pytest.param(
        {
            "read_jira_issue": source_issue(),
            "inspect_jira_context": jira_context(),
            "validate_requirement": {"status": ResultStatus.READY_FOR_JIRA.value},
            "generate_work_breakdown": {"error": "Model output failed schema validation"},
        },
        id="breakdown-failed",
    ),
    pytest.param(
        {
            "read_jira_issue": source_issue(),
            "inspect_jira_context": jira_context(),
            "validate_requirement": {"status": ResultStatus.READY_FOR_JIRA.value},
            "generate_work_breakdown": {
                "breakdown": breakdown(),
                "counts": {},
                "duplicate_matches": [
                    {
                        "proposed_title": "Receive email notifications",
                        "existing_key": "KS-9",
                        "existing_summary": "Email notifications",
                        "score": 0.9,
                    }
                ],
            },
        },
        id="duplicate",
    ),
    pytest.param(
        {
            "read_jira_issue": source_issue(),
            "inspect_jira_context": jira_context(),
            "validate_requirement": {"status": ResultStatus.READY_FOR_JIRA.value},
            "generate_work_breakdown": {
                "breakdown": breakdown(),
                "counts": {},
                "duplicate_matches": [],
            },
            "create_jira_issues": {
                "status": ResultStatus.JIRA_CREATION_FAILED.value,
                "failure_reason": "Jira rejected the Story.",
                "created_keys": [],
            },
        },
        id="creation-failed",
    ),
]


@pytest.mark.parametrize("results", _STOPS_NEEDING_A_PERSON)
async def test_every_stop_that_needs_a_person_marks_the_ticket(tools, results) -> None:
    recorder = tools(**results, **DEFAULTS)

    await JiraTaskCreation.fn({"issue_key": "KS-12"})

    report = recorder.report()
    assert report["mark_awaiting"] is True, "a ticket waiting on a person must say so"
    assert report["mark_processed"] is False


@pytest.mark.parametrize("results", _STOPS_NEEDING_A_PERSON)
async def test_every_stop_leaves_exactly_one_reply(tools, results) -> None:
    recorder = tools(**results, **DEFAULTS)

    await JiraTaskCreation.fn({"issue_key": "KS-12"})

    replies = [name for name in recorder.names() if name == "report_to_issue"]
    assert len(replies) == 1, "exactly one reply comment per run, whatever happened"


@pytest.mark.parametrize("results", _STOPS_NEEDING_A_PERSON)
async def test_every_stop_answers_the_comment_that_asked(tools, results) -> None:
    recorder = tools(**results, **DEFAULTS)

    await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert recorder.report()["answered_comment_id"] == "9001"


# --- delegated mode: the router owns the reply -------------------------------
#
# The webhook tests above assert the agent writes back. These assert the exact
# opposite under `mode="delegated"`, driven through the SAME entry point and the
# same fixtures — which is the only way to know the two modes really share one
# implementation rather than having quietly forked.


async def test_delegated_mode_creates_issues_but_writes_nothing_else(tools) -> None:
    """The whole point: the hierarchy is still made, the reply is not."""
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={
            "status": ResultStatus.READY_FOR_JIRA.value,
            "requirement": "x",
            "request_text": "Let drivers log a rest break",
        },
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["KS-30"],
            "summary": "ok",
        },
        **DEFAULTS,
    )

    result = await JiraTaskCreation.fn({"issue_key": "KS-12", "mode": "delegated"})

    assert not recorder.ran("report_to_issue"), "the router posts the reply, not the agent"
    assert not recorder.ran("notify_email"), "the router sends the email, not the agent"
    # It is still the breakdown agent: creation happened exactly as normal.
    assert recorder.ran("create_jira_issues")
    assert result["produced_by"] == "JiraTaskCreation"
    assert result["display_name"] == "JiraTaskCreation"


async def test_the_same_request_in_direct_mode_still_writes_back(tools) -> None:
    """The control. If this ever fails, the gate was applied to everyone."""
    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={
            "status": ResultStatus.READY_FOR_JIRA.value,
            "requirement": "x",
            "request_text": "Let drivers log a rest break",
        },
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["KS-30"],
            "summary": "ok",
        },
        **DEFAULTS,
    )

    await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert recorder.ran("report_to_issue")


async def test_delegated_mode_carries_the_routers_run_id(tools) -> None:
    """The same id must appear in the comment, the email and every log line."""
    tools(read_jira_issue=source_issue(), **DEFAULTS)

    result = await JiraTaskCreation.fn(
        {"issue_key": "KS-12", "mode": "delegated", "run_id": "4a81f7c2"}
    )

    assert result["run_id"] == "4a81f7c2"
    assert result["contract_version"]


async def test_delegated_mode_returns_a_contract_the_router_can_parse(tools) -> None:
    tools(read_jira_issue=source_issue(), **DEFAULTS)

    data = await JiraTaskCreation.fn({"issue_key": "KS-12", "mode": "delegated"})
    revived = AgentResult.from_dict(data)

    assert revived.produced_by == "JiraTaskCreation"
    assert isinstance(revived.sources_read, list)
    assert isinstance(revived.sources_missing, list)


async def test_a_silent_stop_is_still_silent_in_delegated_mode(tools) -> None:
    """An issue without the keyword must not become a router reply either."""
    recorder = tools(read_jira_issue=source_issue(trigger_matched=False), **DEFAULTS)

    await JiraTaskCreation.fn({"issue_key": "KS-12", "mode": "delegated"})

    assert not recorder.ran("report_to_issue")
    assert not recorder.ran("notify_email")


async def test_delegated_mode_does_not_leak_into_the_next_run(tools) -> None:
    """The mode is a ContextVar. A leak would silence write-back for everyone."""
    tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={
            "status": ResultStatus.READY_FOR_JIRA.value,
            "requirement": "x",
            "request_text": "Let drivers log a rest break",
        },
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["KS-30"],
            "summary": "ok",
        },
        **DEFAULTS,
    )
    await JiraTaskCreation.fn({"issue_key": "KS-12", "mode": "delegated"})

    recorder = tools(
        read_jira_issue=source_issue(),
        inspect_jira_context=jira_context(),
        validate_requirement={
            "status": ResultStatus.READY_FOR_JIRA.value,
            "requirement": "x",
            "request_text": "Let drivers log a rest break",
        },
        generate_work_breakdown={"breakdown": breakdown(), "counts": {}, "duplicate_matches": []},
        create_jira_issues={
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["KS-30"],
            "summary": "ok",
        },
        **DEFAULTS,
    )
    await JiraTaskCreation.fn({"issue_key": "KS-12"})

    assert recorder.ran("report_to_issue"), "direct mode was silenced by a previous run"
