"""One ticket, one job at a time — and an answer the moment a request arrives.

BGV-32 (2026-09-29): "@Aetherion review" got no reply for minutes, so the
person asked "@Aetherion build" too, and nothing told them either was running.
Now every request is answered at once, the same reply becomes the result, and
the two are sequenced on the ticket: the build waits for the review, then uses
what the review found.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.router_flow import run_router
from routing.orchestration import Next, TicketState, clock, plan_next
from shared.contract import AgentResult, Outcome
from tests.fake_tools import Tools
from tools.tools import _storable, content_fingerprint

FINGERPRINT = "f1f1f1f1"
PAYLOAD = {"issue_key": "BGV-32", "comment_id": "10501"}


def review_result(score: int, questions: list[str] | None = None) -> dict[str, Any]:
    return AgentResult(
        produced_by="JiraRequirementReview",
        display_name="Requirement Review",
        outcome=Outcome.REVIEWED,
        headline="Needs major clarification",
        summary=f"Readiness {score}/5.",
        readiness_score=score,
        questions=questions or ["How is the employer contacted?"],
    ).to_dict()


def state(active: str = "", review_score: int = 0, fingerprint: str = FINGERPRINT) -> dict:
    data: dict[str, Any] = {"last": {}}
    if active:
        data["active"] = {
            "intent": active,
            "run_id": "older",
            "started": "2026-09-29T10:00:12+00:00",
            "reply_id": "20000",
        }
    if review_score:
        data["last"]["REVIEW"] = {
            "result": review_result(review_score),
            "fingerprint": fingerprint,
            "finished": "2026-09-29T10:02:00+00:00",
        }
    return data


def gate(comment: str, orchestration: dict | None = None, **over: Any) -> dict[str, Any]:
    return {
        "outcome": "proceed",
        "run_id": "r2",
        "issue_key": "BGV-32",
        "comment_id": "10501",
        "comment_body": comment,
        "issue_summary": "Employer verification",
        "idempotency_key": "comment_created:10501",
        "trigger_keyword": "Aetherion",
        "processed_label": "aetherion-processed",
        "task_queue_overrides": {},
        "fingerprint": FINGERPRINT,
        "orchestration": orchestration or {},
        "build_min_readiness": 3,
        "queue_wait_minutes": 10,
        **over,
    }


class Children:
    def __init__(self, result: dict | None = None) -> None:
        self.result = (
            result
            or AgentResult(
                produced_by="JiraTaskCreation",
                display_name="Work Breakdown",
                outcome=Outcome.CREATED,
                headline="BGV-32 is now a Story",
                summary="BGV-32 is now a Story (it was a Task).",
            ).to_dict()
        )
        self.calls: list[tuple[str, dict]] = []

    async def dispatch(self, agent_name: str, payload: dict, **options: Any) -> dict:
        self.calls.append((agent_name, payload))
        return self.result


class Clock:
    """A sleep that only counts."""

    def __init__(self) -> None:
        self.slept = 0.0

    async def sleep(self, seconds: float) -> None:
        self.slept += seconds


def text(blocks: Any) -> str:
    return str(list(blocks))


# --- the rules, alone ----------------------------------------------------------------


@pytest.mark.parametrize(
    "intent,st,expected",
    [
        ("REVIEW", state(), Next.RUN),
        ("REVIEW", state(active="REVIEW"), Next.ALREADY_RUNNING),
        ("BUILD", state(active="REVIEW"), Next.WAIT),
        ("REVIEW", state(active="BUILD"), Next.WAIT),
        ("REVIEW", state(review_score=4), Next.REUSE_REVIEW),
        ("REVIEW", state(review_score=4, fingerprint="edited"), Next.RUN),
        ("BUILD", state(review_score=2), Next.BUILD_PAUSED),
        ("BUILD", state(review_score=2, fingerprint="edited"), Next.RUN),
        ("BUILD", state(review_score=4), Next.RUN),
        ("QUESTION", state(active="BUILD"), Next.RUN),
    ],
)
def test_what_the_orchestrator_does_next(intent, st, expected) -> None:
    plan = plan_next(intent, TicketState.from_dict(st), FINGERPRINT, 3)
    assert plan.next is expected


def test_a_build_after_a_good_review_carries_its_questions() -> None:
    plan = plan_next("BUILD", TicketState.from_dict(state(review_score=4)), FINGERPRINT, 3)
    assert plan.review_questions == ["How is the employer contacted?"]


def test_the_time_a_person_reads() -> None:
    assert clock("2026-09-29T10:00:12+00:00") == "10:00 UTC"


# --- the flow ----------------------------------------------------------------------------


async def test_a_job_is_answered_at_once_and_the_same_reply_becomes_the_result() -> None:
    tools = Tools(ingress_check=gate("@Aetherion review"))
    children = Children(review_result(3))
    await run_router(PAYLOAD, tools.execute, children.dispatch, Clock().sleep)

    names = tools.names()
    assert names.index("post_reply") < names.index("claim_ticket") < names.index("finish_ticket")
    assert tools.count("post_reply") == 1, "one reply per comment"
    assert "⏳ Reviewing BGV-32" in text(tools.args("post_reply")[1])
    assert "Needs major clarification" in tools.reply_text()
    _issue, intent, _run, result, fingerprint = tools.args("finish_ticket")
    assert (intent, fingerprint) == ("REVIEW", FINGERPRINT)
    assert result["readiness_score"] == 3


async def test_the_same_job_twice_starts_nothing() -> None:
    tools = Tools(ingress_check=gate("@Aetherion review", state(active="REVIEW")))
    children = Children()
    await run_router(PAYLOAD, tools.execute, children.dispatch, Clock().sleep)
    assert children.calls == []
    assert "Already reviewing BGV-32" in tools.reply_text()
    assert "10:00 UTC" in tools.reply_text()


async def test_bgv32_a_build_during_a_review_waits_then_runs_on_the_same_ticket() -> None:
    reads = iter([{"state": state(active="REVIEW")}, {"state": state(review_score=4)}])
    tools = Tools(
        ingress_check=gate("@Aetherion build", state(active="REVIEW")),
        ticket_state=lambda *_: next(reads),
    )
    children = Children()
    clock_ = Clock()
    await run_router(PAYLOAD, tools.execute, children.dispatch, clock_.sleep)

    assert "Build queued for BGV-32" in text(tools.args("post_reply")[1])
    assert "review of BGV-32 that started at 10:00 UTC" in text(tools.args("post_reply")[1])
    assert clock_.slept == 20, "it waited for the review, checking every 10 s"
    [(agent, payload)] = children.calls
    assert agent == "JiraTaskCreation" and payload["issue_key"] == "BGV-32"
    assert payload["review_questions"] == ["How is the employer contacted?"]
    assert tools.count("post_reply") == 1
    assert "BGV-32 is now a Story" in tools.reply_text()


async def test_a_queued_build_pauses_when_the_review_ahead_scored_low() -> None:
    reads = iter([{"state": state(review_score=2)}])
    tools = Tools(
        ingress_check=gate("@Aetherion build", state(active="REVIEW")),
        ticket_state=lambda *_: next(reads),
    )
    children = Children()
    await run_router(PAYLOAD, tools.execute, children.dispatch, Clock().sleep)
    assert children.calls == []
    assert "Build paused — the review scored BGV-32 2/5" in tools.reply_text()


async def test_a_review_of_an_unchanged_ticket_is_reused_without_the_model() -> None:
    tools = Tools(ingress_check=gate("@Aetherion review", state(review_score=4)))
    children = Children()
    await run_router(PAYLOAD, tools.execute, children.dispatch, Clock().sleep)
    assert children.calls == []
    assert "has not changed since the last review" in tools.reply_text()
    assert "Needs major clarification" in tools.reply_text()


async def test_a_build_after_a_poor_review_is_paused_with_the_questions() -> None:
    tools = Tools(ingress_check=gate("@Aetherion build", state(review_score=2)))
    children = Children()
    await run_router(PAYLOAD, tools.execute, children.dispatch, Clock().sleep)
    assert children.calls == []
    reply = tools.reply_text()
    assert "Build paused" in reply and "How is the employer contacted?" in reply
    assert "Nothing was created" in reply


async def test_an_edited_ticket_builds_whatever_the_old_review_said() -> None:
    tools = Tools(
        ingress_check=gate("@Aetherion build", state(review_score=2, fingerprint="before-edit"))
    )
    children = Children()
    await run_router(PAYLOAD, tools.execute, children.dispatch, Clock().sleep)
    assert len(children.calls) == 1
    assert "review_questions" not in children.calls[0][1]


async def test_a_reply_that_cannot_be_edited_is_posted_instead() -> None:
    tools = Tools(ingress_check=gate("@Aetherion review"), update_reply={"updated": False})
    await run_router(PAYLOAD, tools.execute, Children(review_result(4)).dispatch, Clock().sleep)
    assert tools.count("post_reply") == 2
    assert "Needs major clarification" in tools.reply_text()


async def test_help_is_still_answered_directly() -> None:
    tools = Tools(ingress_check=gate("@Aetherion help"))
    children = Children()
    await run_router(PAYLOAD, tools.execute, children.dispatch, Clock().sleep)
    assert children.calls == [] and tools.count("claim_ticket") == 0
    assert "What I can do" in tools.reply_text()


# --- what is kept ------------------------------------------------------------------------


def test_the_fingerprint_ignores_what_the_agent_itself_changes() -> None:
    fields = {"summary": "S", "description": {"type": "doc"}, "attachment": [{"id": 1}]}
    labelled = {**fields, "labels": ["aetherion-reviewed"], "comment": {"total": 9}}
    assert content_fingerprint(fields) == content_fingerprint(labelled)
    assert content_fingerprint(fields) != content_fingerprint({**fields, "summary": "T"})


def test_a_huge_result_is_kept_short_enough_for_jira() -> None:
    big = review_result(3) | {"findings": [{"description": "x" * 1000}] * 60}
    kept = _storable(big)
    assert "findings" not in kept and kept["readiness_score"] == 3
    assert _storable(review_result(3)) == review_result(3)


def test_a_failed_review_is_never_reused() -> None:
    st = state(review_score=4)
    st["last"]["REVIEW"]["result"]["outcome"] = "FAILED"
    plan = plan_next("REVIEW", TicketState.from_dict(st), FINGERPRINT, 3)
    assert plan.next is Next.RUN
