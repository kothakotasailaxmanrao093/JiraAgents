"""A bug is investigated, never broken down (2026-10-22).

"@Aetherion <a description of something broken>" was classified BUILD and sent
to the work-breakdown agent, which created a Task with Sub-tasks; "@Aetherion
build" on a Bug created Sub-tasks under it. The router did not even read the
ticket's type. These tests pin the new routes, and that every other route —
Stories, the TDD and code commands, help — is unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.router_flow import run_router
from routing.catalog import by_intent
from routing.classify import (
    BUG_NOT_BUILT,
    WORKFLOW_INTENTS,
    classify,
    classify_by_verb,
    for_issue_type,
)
from routing.compose import compose
from shared.contract import AgentResult, EmailKind, Outcome
from tests.fake_tools import Tools
from tests.test_router_flow import PAYLOAD, Children, created_contract, gate, reviewed_contract
from tools.tools import _parse_scores

# --- routing rules, pure -------------------------------------------------------


def test_review_on_a_bug_becomes_an_investigation() -> None:
    decision = for_issue_type(classify_by_verb("@Aetherion review"), "Bug")
    assert decision.intent == "BUG"
    assert decision.agent is not None
    assert decision.agent.agent_name == "JiraRequirementReview"
    assert decision.agent.forced_payload["review_kind"] == "bug"
    assert decision.agent.forced_payload["post_to_jira"] is False
    assert "is a Bug" in decision.reason


def test_build_on_a_bug_is_refused_by_the_router_itself() -> None:
    decision = for_issue_type(classify_by_verb("@Aetherion build"), "Bug")
    assert decision.intent == BUG_NOT_BUILT
    assert decision.agent is None  # nothing is dispatched, so nothing can be created


@pytest.mark.parametrize("issue_type", ["Story", "Task", "Epic", "", None])
def test_other_card_types_route_exactly_as_before(issue_type) -> None:
    for body, intent in (("@Aetherion build", "BUILD"), ("@Aetherion review", "REVIEW")):
        assert for_issue_type(classify_by_verb(body), issue_type).intent == intent


@pytest.mark.parametrize(
    "body", ["@Aetherion create TDD", "@Aetherion code", "@Aetherion help", "@Aetherion status"]
)
def test_explicit_commands_on_a_bug_are_unchanged(body) -> None:
    before = classify_by_verb(body)
    after = for_issue_type(before, "Bug")
    assert after.intent == before.intent
    if before.intent in WORKFLOW_INTENTS:
        assert after.intent in WORKFLOW_INTENTS


@pytest.mark.parametrize(
    "body", ["@Aetherion investigate", "@Aetherion debug this", "@Aetherion find the root cause"]
)
def test_investigate_verbs_route_to_the_bug_investigation(body) -> None:
    decision = classify_by_verb(body)
    assert decision is not None and decision.intent == "BUG" and decision.layer == 1


def test_the_classifier_can_answer_bug() -> None:
    scores = _parse_scores('{"BUILD": 0.1, "REVIEW": 0.1, "BUG": 0.75, "QUESTION": 0.05}')
    assert scores["BUG"] == 0.75
    decision = classify(
        "@Aetherion the transfer API returns the wrong balance, please look", scores=scores
    )
    assert decision.intent == "BUG"
    assert decision.agent is by_intent("BUG")


def test_an_unsure_build_review_split_on_a_bug_investigates_rather_than_builds() -> None:
    decision = classify("@Aetherion look at this", scores={"BUILD": 0.45, "REVIEW": 0.40})
    assert decision.intent == "REVIEW"  # the reversible guess
    assert for_issue_type(decision, "Bug").intent == "BUG"


# --- the whole router, with fakes ----------------------------------------------


def _bug_gate(body: str, **over: Any) -> dict[str, Any]:
    return gate(comment_body=body, issue_type="Bug", issue_summary="Wrong balance", **over)


async def test_build_on_a_bug_dispatches_nothing_and_replies_once() -> None:
    tools = Tools(ingress_check=_bug_gate("@Aetherion build"))
    children = Children(result=created_contract())

    result = await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert children.calls == []  # the work-breakdown agent never ran
    assert tools.count("post_reply") == 1
    assert result["created"] == []
    text = tools.reply_text()
    assert "Nothing was created — this ticket is a Bug" in text
    assert "@Aetherion investigate" in text and "@Aetherion create TDD" in text


async def test_review_on_a_bug_runs_the_investigation_not_the_breakdown() -> None:
    tools = Tools(ingress_check=_bug_gate("@Aetherion review"))
    children = Children(result=reviewed_contract())

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    # Review judges the report, then Planning looks for the cause (2026-10-26).
    assert [c[0] for c in children.calls] == ["JiraRequirementReview", "planning_agent"]
    agent_name, payload, options = children.calls[0]
    assert payload["review_kind"] == "bug" and payload["post_to_jira"] is False
    assert options["workflow_id"].startswith("bug-")
    assert children.calls[1][1]["mode"] == "investigate"


async def test_a_build_on_a_story_still_builds() -> None:
    tools = Tools(ingress_check=gate(comment_body="@Aetherion build", issue_type="Story"))
    children = Children(result=created_contract())

    result = await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert children.calls[0][0] == "JiraTaskCreation"
    assert result["created"] == ["FL-121"]


# --- the reply shows the investigation ------------------------------------------


def _investigated(score: int = 4, **investigation: Any) -> AgentResult:
    return AgentResult(
        produced_by="JiraRequirementReview",
        display_name="JiraRequirementReview",
        outcome=Outcome.REVIEWED,
        headline="Bug investigation — likely cause found",
        summary="Needs minor clarification (4/5). The balance is read before the debit commits.",
        readiness_score=score,
        investigation=investigation,
        sources_read=["BTS-30 description"],
        email_kind=EmailKind.NONE,
    )


def _text(blocks) -> str:
    out = []
    for heading, body in blocks:
        out.append(str(heading))
        out.extend(str(x) for x in body) if isinstance(body, list) else out.append(str(body))
    return "\n".join(out)


def test_the_reply_is_short_cause_code_fix_and_next() -> None:
    result = _investigated(
        actual="The balance shows the old amount",
        expected="The balance shows the amount after the transfer",
        root_cause="getBalance() reads a cached Account before the debit is saved.",
        root_cause_confidence="high",
        evidence=["TransferService.transfer saves after returning (Code …/TransferService.java)"],
        affected_files=[{"path": "Org/repo/src/TransferService.java", "why": "saves late"}],
        proposed_fix="Save the debit before reading the balance.",
        missing_information=["The exact request that showed the stale balance"],
        code_note="Code searched for: transfer, balance.",
    )
    blocks = compose(result, routed_to="bug", ran=["Bug Investigation"])
    headings = [h for h, _ in blocks if h]
    assert headings == [
        "AetherionAgent · Bug investigation — likely cause found",
        "Readiness",
        "What is wrong",
        "Likely cause (high confidence)",
        "Code",
        "Proposed fix — not applied",
        "Next",
    ]
    text = _text(blocks)
    assert "Actual: The balance shows the old amount" in text
    assert "Org/repo/src/TransferService.java — saves late" in text
    # Ready (4/5): nothing more is asked (BTS-40, 2026-11-02); no evidence,
    # findings, sources or routing lines.
    assert "exact request" not in text and "Evidence" not in text
    assert "Routed to" not in text and "What I read" not in text and "Findings" not in text


def test_a_report_that_is_not_ready_lists_what_is_needed_once() -> None:
    result = _investigated(
        score=2,
        root_cause="Unknown without the steps to reproduce.",
        missing_information=["The steps that lead to the stale balance"],
    )
    text = _text(compose(result, routed_to="bug", ran=["Bug Investigation"]))
    assert text.count("The steps that lead to the stale balance") == 1
    assert "Still needed" in text


def test_code_not_found_is_one_short_line() -> None:
    # BTS-40, live: the note listed every word searched for.
    result = _investigated(
        root_cause="The toolbar mounts ARCHIVE only on selection.",
        code_note=(
            "The code for this bug was not found in the repositories Planning has "
            "(FirstRepo, bank, codepulse-springboot-test); searched for: Candidate Status, "
            "First Name. The cause above comes from the report."
        ),
    )
    text = _text(compose(result, routed_to="bug", ran=["Bug Investigation"]))
    assert (
        "Not found in the connected repositories: FirstRepo, bank, codepulse-springboot-test."
        in text
    )
    assert "searched for" not in text


def test_an_ordinary_review_has_no_investigation_sections() -> None:
    result = _investigated()
    text = _text(compose(result, routed_to="review", ran=["Requirement Review"]))
    for heading in ("What is wrong", "Likely cause", "Code", "Proposed fix"):
        assert heading not in text
