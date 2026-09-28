"""Delegated mode — the agent as a pure function the router calls.

The breakdown itself is covered by the rest of the suite. What matters here is
the handover, because every one of these is something the routed architecture
depends on and none of it is visible from the breakdown output:

* it still CREATES the Jira issues — that is the agent's purpose;
* it posts no reply, stamps no label, records no answered comment, sends no
  email, at any of the twelve exits;
* it returns the shared contract, fully attributed;
* the guarantees that move to the router are handed over rather than dropped.

Direct mode is asserted here too, against the same fixtures, because "delegated
mode changed nothing for existing callers" is the claim that matters most.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agent import agent as agent_mod
from src.agent.delegated import AGENT_DISPLAY_NAME, AGENT_NAME, to_contract
from src.models.schemas import ResultStatus
from src.shared.contract import AgentResult, EmailKind, Outcome


@pytest.fixture(autouse=True)
def _reset_mode():
    """The mode is a ContextVar; leaking it between tests would hide bugs."""
    token = agent_mod._DELEGATED.set(False)
    yield
    agent_mod._DELEGATED.reset(token)


def _created_result(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": ResultStatus.JIRA_CREATED.value,
        "message": "Created 1 Story and 3 Sub-tasks.",
        "summary_text": "Created 1 Story and 3 Sub-tasks.",
        "generator": "model",
        # The shape create_jira_issues really returns (JiraResult). This
        # fixture used to carry a "created" list the real result never has,
        # so the test passed while no live reply listed what was created.
        "jira_result": {
            "status": ResultStatus.JIRA_CREATED.value,
            "epic": None,
            "stories": [
                {
                    "key": "FL-121",
                    "issue_type": "Story",
                    "summary": "Record a tyre pressure check",
                    "url": "https://x/browse/FL-121",
                }
            ],
            "subtasks": [
                {"key": "FL-122", "issue_type": "Sub-task", "summary": "Define the rules"}
            ],
            "created_keys": ["FL-121", "FL-122"],
        },
        "source_issue": {
            "key": "FL-120",
            "description": "Let depot staff record a tyre pressure check.",
            "comments": [{"body": "a"}, {"body": "b"}, {"body": "c"}],
            "attachments": [{"filename": "spec_v1.pdf", "text": "the spec"}],
            "confluence_pages": [],
            "linked_issues": [],
        },
        "jira_context": {"issue_count": 111},
        "notification": {"attempted": False, "suppressed": True, "kind": "created"},
        "duplicate_matches": [],
        "clarifying_questions": [],
        "validation_errors": [],
    }
    base.update(over)
    return base


# --- the write-gates ---------------------------------------------------------


async def test_the_reply_comment_is_not_posted_in_delegated_mode() -> None:
    """The router posts exactly one comment. A child that also posts makes
    "exactly once" a promise three services have to keep."""
    agent_mod._DELEGATED.set(True)
    called: list[Any] = []

    async def _execute(*args: Any, **kwargs: Any) -> dict:  # pragma: no cover
        called.append(args)
        return {}

    original = agent_mod.toolExecutor.execute
    agent_mod.toolExecutor.execute = _execute  # type: ignore[assignment]
    try:
        result = await agent_mod._report("FL-120", headline="x", mark_processed=True)
    finally:
        agent_mod.toolExecutor.execute = original  # type: ignore[assignment]

    assert result == {}
    assert called == [], "report_to_issue must not be invoked in delegated mode"


async def test_the_reply_comment_is_still_posted_in_direct_mode() -> None:
    """The guard must not have switched write-back off for everyone."""
    called: list[Any] = []

    async def _execute(*args: Any, **kwargs: Any) -> dict:
        called.append(args[0])
        return {"ok": True}

    original = agent_mod.toolExecutor.execute
    agent_mod.toolExecutor.execute = _execute  # type: ignore[assignment]
    try:
        await agent_mod._report("FL-120", headline="x")
    finally:
        agent_mod.toolExecutor.execute = original  # type: ignore[assignment]

    assert called == ["report_to_issue"]


async def test_no_email_is_sent_in_delegated_mode_but_the_intent_is_recorded() -> None:
    """The child recommends; the router decides and sends."""
    agent_mod._DELEGATED.set(True)
    called: list[Any] = []

    async def _execute(*args: Any, **kwargs: Any) -> dict:  # pragma: no cover
        called.append(args)
        return {}

    original = agent_mod.toolExecutor.execute
    agent_mod.toolExecutor.execute = _execute  # type: ignore[assignment]
    try:
        from src.models.schemas import NotificationKind

        result = await agent_mod._notify(NotificationKind.CREATED, issue_key="FL-120")
    finally:
        agent_mod.toolExecutor.execute = original  # type: ignore[assignment]

    assert called == [], "notify_email must not be invoked in delegated mode"
    assert result["suppressed"] is True
    assert result["kind"] == "created"


async def test_manual_mode_still_writes_nothing_back() -> None:
    """Unrelated to delegation, and it must stay true."""
    assert await agent_mod._report("", headline="x") == {}


# --- the contract ------------------------------------------------------------


def test_a_created_run_is_attributed_and_lists_what_it_made() -> None:
    result = to_contract(_created_result(), run_id="4a81f7c2", agent_version="4.2.0")

    assert result.produced_by == AGENT_NAME
    assert result.display_name == AGENT_DISPLAY_NAME
    assert result.agent_version == "4.2.0"
    assert result.run_id == "4a81f7c2"
    assert result.outcome is Outcome.CREATED
    assert [c.key for c in result.created] == ["FL-121", "FL-122"]
    assert result.created[0].issue_type == "Story"


def test_the_contract_survives_a_round_trip_as_the_router_will_receive_it() -> None:
    data = to_contract(_created_result(), run_id="r1", agent_version="4.2.0").to_dict()
    revived = AgentResult.from_dict(data)
    assert revived.outcome is Outcome.CREATED
    assert revived.created[0].key == "FL-121"


def test_what_was_read_is_named() -> None:
    result = to_contract(_created_result(), run_id="r1", agent_version="4.2.0")
    read = " | ".join(result.sources_read)
    assert "FL-120 description" in read
    assert "3 comment(s)" in read
    assert "Attachment: spec_v1.pdf" in read
    assert "111 existing ticket(s)" in read


def test_an_unreadable_attachment_is_reported_with_what_to_do() -> None:
    """A thin breakdown must never be mistakable for a badly written ticket."""
    source = _created_result()
    source["source_issue"]["attachments"] = [
        {"filename": "spec.xlsx", "text": "", "note": "password-protected"}
    ]
    result = to_contract(source, run_id="r1", agent_version="4.2.0")

    sentence = " ".join(m.as_sentence() for m in result.sources_missing)
    assert "spec.xlsx" in sentence
    assert "password-protected" in sentence
    assert "re-attach" in sentence
    assert "Attachment: spec.xlsx" not in " | ".join(result.sources_read)


def test_an_unreadable_confluence_page_is_reported_with_what_to_do() -> None:
    source = _created_result()
    source["source_issue"]["confluence_pages"] = [
        {"title": "Depot Ops Runbook", "text": "", "note": "no access"}
    ]
    result = to_contract(source, run_id="r1", agent_version="4.2.0")

    sentence = " ".join(m.as_sentence() for m in result.sources_missing)
    assert "Depot Ops Runbook" in sentence
    assert "grant access" in sentence


def test_a_link_referenced_mid_requirement_is_reported_as_missing() -> None:
    """Bug 6: a link that is not the *whole* request (gate 0c's narrower case)
    but is referenced partway through must still be named, or it silently
    vanishes once the rest of the text passes validation."""
    source = _created_result()
    source["source_issue"]["unread_links"] = ["https://example.com/policy"]
    result = to_contract(source, run_id="r1", agent_version="4.2.0")

    sentence = " ".join(m.as_sentence() for m in result.sources_missing)
    assert "https://example.com/policy" in sentence
    assert "could not be read" in sentence


def test_the_accounting_lists_are_present_even_when_empty() -> None:
    data = to_contract(
        {"status": ResultStatus.READY_FOR_JIRA.value}, run_id="r1", agent_version="4.2.0"
    ).to_dict()
    assert data["sources_read"] == []
    assert data["sources_missing"] == []


# --- the four outcomes -------------------------------------------------------


@pytest.mark.parametrize(
    "status, expected",
    [
        (ResultStatus.JIRA_CREATED, Outcome.CREATED),
        (ResultStatus.CLARIFICATION_REQUIRED, Outcome.NEEDS_INFO),
        (ResultStatus.VALIDATION_ERROR, Outcome.NOT_A_REQUIREMENT),
        (ResultStatus.OUT_OF_SCOPE, Outcome.NOT_A_REQUIREMENT),
        (ResultStatus.EXPLAINED, Outcome.ANSWERED),
        (ResultStatus.JIRA_CREATION_FAILED, Outcome.FAILED),
        (ResultStatus.READY_FOR_JIRA, Outcome.REVIEWED),
    ],
)
def test_every_terminal_status_maps_to_an_outcome(status, expected) -> None:
    """The router's layout table must be exhaustive, so nothing may fall off."""
    result = to_contract({"status": status.value}, run_id="r1", agent_version="4.2.0")
    assert result.outcome is expected


def test_an_unknown_status_fails_safe_rather_than_crashing_the_router() -> None:
    result = to_contract({"status": "SOMETHING_NEW"}, run_id="r1", agent_version="4.2.0")
    assert result.outcome is Outcome.FAILED


def test_duplicates_with_nothing_created_become_already_exists() -> None:
    result = to_contract(
        {
            "status": ResultStatus.READY_FOR_JIRA.value,
            "duplicate_matches": [
                {"proposed_title": "Record a check", "existing_key": "FL-9", "score": 0.81}
            ],
        },
        run_id="r1",
        agent_version="4.2.0",
    )
    assert result.outcome is Outcome.ALREADY_EXISTS
    assert result.duplicates[0].existing_key == "FL-9"
    assert result.duplicates[0].score == 0.81


# --- never email an invalid request ------------------------------------------


@pytest.mark.parametrize("status", [ResultStatus.VALIDATION_ERROR, ResultStatus.OUT_OF_SCOPE])
def test_an_invalid_request_never_recommends_an_email(status) -> None:
    """The rule survives the handover: the child cannot even ask."""
    result = to_contract(
        {
            "status": status.value,
            # Even if a branch somehow recorded one, the recommendation is refused.
            "notification": {"suppressed": True, "kind": "created"},
        },
        run_id="r1",
        agent_version="4.2.0",
    )
    assert result.email_recommended is False
    assert result.email_kind is EmailKind.NONE


def test_an_invalid_request_kind_is_not_mappable_at_all() -> None:
    """Belt and braces: "invalid_request" is deliberately absent from the map."""
    result = to_contract(
        {
            "status": ResultStatus.JIRA_CREATION_FAILED.value,
            "notification": {"suppressed": True, "kind": "invalid_request"},
        },
        run_id="r1",
        agent_version="4.2.0",
    )
    assert result.email_recommended is False


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("created", EmailKind.CREATED),
        ("clarification_required", EmailKind.CLARIFICATION),
        ("duplicates_found", EmailKind.DUPLICATES),
        ("partial_duplicate", EmailKind.DUPLICATES),
        ("failed", EmailKind.FAILED),
    ],
)
def test_a_legitimate_email_is_recommended_with_its_kind(kind, expected) -> None:
    result = to_contract(
        {
            "status": ResultStatus.JIRA_CREATED.value,
            "notification": {"suppressed": True, "kind": kind},
        },
        run_id="r1",
        agent_version="4.2.0",
    )
    assert result.email_recommended is True
    assert result.email_kind is expected


# --- degraded ----------------------------------------------------------------


def test_a_heuristic_run_is_flagged_degraded_for_the_router_to_render() -> None:
    """The model's answer could not be used and the wording is wooden. The
    reader must be told, and the router places the warning. Not "unreachable":
    the fallback also runs when the model answered with something unusable."""
    result = to_contract(_created_result(generator="heuristic"), run_id="r1", agent_version="4.2.0")
    assert result.degraded is True
    assert "could not be used" in result.degraded_reason
    assert "unreachable" not in result.degraded_reason


def test_a_normal_run_is_not_flagged_degraded() -> None:
    result = to_contract(_created_result(), run_id="r1", agent_version="4.2.0")
    assert result.degraded is False
    assert result.degraded_reason == ""
