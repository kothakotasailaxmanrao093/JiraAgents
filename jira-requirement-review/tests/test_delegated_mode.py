"""Delegated mode — the agent as a pure function the router calls.

Three properties matter more than the review itself here, because each one is
something the routed architecture depends on and none of them is visible from
the review output:

1. it writes NOTHING to Jira, whatever the payload or the configuration says;
2. it returns the shared result contract, fully attributed;
3. it accounts for what it read and what it could not, on every run.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.delegated import run_delegated
from config import AGENT_DISPLAY_NAME, AGENT_NAME
from shared.contract import AgentResult, EmailKind, Outcome


def _review_result(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "success",
        "issue_key": "ABC-1",
        "summary": "Record a tyre pressure check",
        "finding_count": 2,
        "findings": [
            {
                "category": "ambiguity",
                "description": '"real-time" is used with no latency target.',
            },
            {
                "category": "open_question",
                "description": "What happens when the reading is out of range?",
            },
        ],
        "readiness": {"level": "needs_major_clarification", "score": 2},
        "warnings": [],
        "documents": [
            {
                "source_label": "Target ABC-1 (description)",
                "kind": "requirement",
                "text": "Drivers log a break.",
            }
        ],
        "target_bundle": {"key": "ABC-1", "description_text": "Drivers log a break."},
    }
    base.update(over)
    return base


def _runner(results: list[dict] | Exception):
    """A stand-in for ``run_review`` that records what it was given."""
    seen: dict[str, Any] = {}

    async def run(payload, execute, workflow_id=None):
        seen["payload"] = payload
        seen["workflow_id"] = workflow_id
        if isinstance(results, Exception):
            raise results
        return results

    run.seen = seen  # type: ignore[attr-defined]
    return run


async def _execute(*args: Any, **kwargs: Any) -> dict:  # pragma: no cover
    raise AssertionError("delegated mode must not call activities directly")


# --- it writes nothing -------------------------------------------------------


@pytest.mark.parametrize("requested", [True, False, "true", 1])
async def test_publishing_is_forced_off_however_it_was_requested(requested: Any) -> None:
    """A webhook is an unsolicited trigger. It must not turn a review tool into
    an auto-commenter, and no payload or configuration may re-enable it."""
    run = _runner([_review_result()])
    await run_delegated(
        {"issue_key": "ABC-1", "post_to_jira": requested, "run_id": "r1"},
        _execute,
        run_review=run,
    )
    assert run.seen["payload"]["post_to_jira"] is False


async def test_the_word_report_is_also_off() -> None:
    """A .docx upload is a side effect nobody asked for over a webhook."""
    run = _runner([_review_result()])
    await run_delegated(
        {"issue_key": "ABC-1", "generate_docx": True, "run_id": "r1"}, _execute, run_review=run
    )
    assert run.seen["payload"]["generate_docx"] is False


async def test_a_batch_query_is_ignored_because_a_webhook_is_one_issue() -> None:
    run = _runner([_review_result()])
    await run_delegated(
        {"issue_key": "ABC-1", "jql_override": "project = FL", "run_id": "r1"},
        _execute,
        run_review=run,
    )
    assert run.seen["payload"]["jql_override"] == ""


# --- it returns the contract, attributed -------------------------------------


async def test_the_result_is_the_shared_contract_fully_attributed() -> None:
    run = _runner([_review_result()])
    payload = {"issue_key": "ABC-1", "run_id": "9c03be57", "comment_id": "10501"}

    data = await run_delegated(payload, _execute, run_review=run)
    result = AgentResult.from_dict(data)

    assert result.produced_by == AGENT_NAME
    assert result.display_name == AGENT_DISPLAY_NAME
    assert result.run_id == "9c03be57"
    assert result.agent_version and result.agent_version != "unknown"
    assert result.outcome is Outcome.REVIEWED


async def test_a_review_never_claims_to_have_created_anything() -> None:
    """The router renders "Created in Jira" from this list."""
    run = _runner([_review_result()])
    result = AgentResult.from_dict(
        await run_delegated({"issue_key": "ABC-1"}, _execute, run_review=run)
    )
    assert result.created == []
    assert result.duplicates == []


async def test_a_review_never_recommends_an_email() -> None:
    """It created nothing, so it has nothing to announce. The router decides,
    but this child must not ask."""
    run = _runner([_review_result()])
    result = AgentResult.from_dict(
        await run_delegated({"issue_key": "ABC-1"}, _execute, run_review=run)
    )
    assert result.email_recommended is False
    assert result.email_kind is EmailKind.NONE


async def test_the_triggering_comment_is_passed_through() -> None:
    run = _runner([_review_result()])
    await run_delegated({"issue_key": "ABC-1", "comment_id": "10501"}, _execute, run_review=run)
    assert run.seen["payload"]["comment_id"] == "10501"


# --- it accounts for everything ---------------------------------------------


async def test_sources_read_and_missing_are_present_even_when_empty() -> None:
    """Mandatory in meaning: may be empty, never absent."""
    run = _runner([_review_result(documents=[], target_bundle={"key": "ABC-1"}, warnings=[])])
    data = await run_delegated({"issue_key": "ABC-1"}, _execute, run_review=run)

    assert "sources_read" in data and isinstance(data["sources_read"], list)
    assert "sources_missing" in data and isinstance(data["sources_missing"], list)


async def test_what_was_read_is_named() -> None:
    run = _runner(
        [
            _review_result(
                documents=[
                    {
                        "source_label": "Target ABC-1 (description)",
                        "kind": "requirement",
                        "text": "x",
                    },
                    {
                        "source_label": "Attachment spec.pdf (on ABC-1)",
                        "kind": "attachment",
                        "text": "y",
                    },
                ]
            )
        ]
    )
    result = AgentResult.from_dict(
        await run_delegated({"issue_key": "ABC-1"}, _execute, run_review=run)
    )

    assert result.sources_read == [
        "Target ABC-1 (description)",
        "Attachment spec.pdf (on ABC-1)",
    ]


async def test_an_unreadable_file_becomes_an_instruction() -> None:
    """Never an apology: the entry has to tell someone what to do."""
    run = _runner(
        [
            _review_result(
                warnings=["spec.xlsx (attached to ABC-1) could not be read: password-protected"]
            )
        ]
    )
    result = AgentResult.from_dict(
        await run_delegated({"issue_key": "ABC-1"}, _execute, run_review=run)
    )

    sentence = " ".join(m.as_sentence() for m in result.sources_missing)
    assert "spec.xlsx" in sentence
    assert "re-attach" in sentence


async def test_a_ticket_with_no_acceptance_criteria_is_told_so() -> None:
    run = _runner([_review_result()])
    result = AgentResult.from_dict(
        await run_delegated({"issue_key": "ABC-1"}, _execute, run_review=run)
    )

    sentence = " ".join(m.as_sentence() for m in result.sources_missing)
    assert "no acceptance criteria" in sentence
    assert "please add them" in sentence


async def test_a_ticket_that_has_acceptance_criteria_is_not_nagged() -> None:
    """A false accusation is worse than a missed nudge."""
    run = _runner(
        [
            _review_result(
                target_bundle={
                    "key": "ABC-1",
                    "description_text": "Log a break.\n\nAcceptance criteria:\n- one",
                }
            )
        ]
    )
    result = AgentResult.from_dict(
        await run_delegated({"issue_key": "ABC-1"}, _execute, run_review=run)
    )

    assert not any("acceptance criteria" in m.what for m in result.sources_missing)


# --- failure still produces a reply -----------------------------------------


async def test_a_missing_issue_key_is_reported_not_crashed() -> None:
    run = _runner([_review_result()])
    result = AgentResult.from_dict(await run_delegated({}, _execute, run_review=run))

    assert result.outcome is Outcome.FAILED
    assert result.errors


async def test_a_crash_becomes_a_degraded_result_the_router_can_render() -> None:
    """The router owns the reply, so a child that explodes must still hand back
    something renderable."""
    run = _runner(RuntimeError("AI Gateway unreachable"))
    result = AgentResult.from_dict(
        await run_delegated({"issue_key": "ABC-1", "run_id": "r9"}, _execute, run_review=run)
    )

    assert result.outcome is Outcome.FAILED
    assert result.degraded is True
    assert "AI Gateway unreachable" in result.degraded_reason
    assert result.run_id == "r9"


async def test_a_ticket_with_nothing_to_review_is_still_a_completed_review() -> None:
    """Not a crash, and not NEEDS_INFO either — that outcome belongs exclusively
    to the work-breakdown agent's "too thin to build" path. A review that finds
    nothing to read is a completed review reporting a finding, not a request
    for more information before the agent can act. See FLAWS.md Bug 8."""
    run = _runner(
        [
            {
                "status": "error",
                "issue_key": "ABC-1",
                "error": "no_requirement_text",
                "warnings": [],
                "documents": [],
                "target_bundle": {"key": "ABC-1", "description_text": ""},
                "message": (
                    "ABC-1 has no description, attachment or linked page " "that could be read."
                ),
            }
        ]
    )
    result = AgentResult.from_dict(
        await run_delegated({"issue_key": "ABC-1"}, _execute, run_review=run)
    )

    assert result.outcome is Outcome.REVIEWED
    sentence = " ".join(m.as_sentence() for m in result.sources_missing)
    assert "no description" in sentence


async def test_an_empty_review_result_is_reported() -> None:
    run = _runner([])
    result = AgentResult.from_dict(
        await run_delegated({"issue_key": "ABC-1"}, _execute, run_review=run)
    )
    assert result.outcome is Outcome.FAILED
    assert result.errors


# --- NEEDS_INFO belongs to the work-breakdown agent, never this one ---------


@pytest.mark.parametrize(
    "review_result",
    [
        _review_result(),
        _review_result(status="error", error="no_requirement_text"),
        _review_result(readiness={"level": "not_ready", "score": 0}, findings=[]),
    ],
    ids=["ordinary review", "nothing to review", "not-ready, no findings"],
)
async def test_a_review_never_returns_needs_info(review_result: dict) -> None:
    """Bug 8: the review agent only assesses what is already written and must
    never ask the router to treat its result as a build-style clarification —
    that outcome is exclusively the work-breakdown agent's to produce."""
    run = _runner([review_result])
    result = AgentResult.from_dict(
        await run_delegated({"issue_key": "ABC-1"}, _execute, run_review=run)
    )
    assert result.outcome is not Outcome.NEEDS_INFO


# --- the Readiness line shows the level, score and summary ------------------


async def test_readiness_shows_level_score_and_the_models_summary() -> None:
    """BGV-11: Readiness read "Read 3 source(s)." — the score and summary live
    inside ``readiness`` and were looked for at the top level."""
    readiness = {
        "level": "needs_minor_clarification",
        "score": 4,
        "executive_summary": "Clear criteria; the 5-day timer start is undefined.",
    }
    run = _runner([_review_result(readiness=readiness)])
    out = await run_delegated({"issue_key": "ABC-1", "run_id": "r1"}, _execute, run_review=run)
    assert out["summary"] == (
        "Needs minor clarification (4/5). Clear criteria; the 5-day timer start is undefined."
    )


async def test_no_readiness_falls_back_to_what_was_read() -> None:
    run = _runner([_review_result(readiness=None)])
    out = await run_delegated({"issue_key": "ABC-1", "run_id": "r1"}, _execute, run_review=run)
    assert "source" in out["summary"]


async def test_the_readiness_score_reaches_the_router_as_a_number() -> None:
    """The router pauses a build after a poor review of an unchanged ticket, so
    it needs the score itself, not a sentence to read it out of (2026-09-29)."""
    data = await run_delegated(
        {"issue_key": "ABC-1"}, _execute, run_review=_runner([_review_result()])
    )
    assert AgentResult.from_dict(data).readiness_score == 2


async def test_no_readiness_is_zero_not_a_guess() -> None:
    data = await run_delegated(
        {"issue_key": "ABC-1"}, _execute, run_review=_runner([_review_result(readiness=None)])
    )
    assert AgentResult.from_dict(data).readiness_score == 0
