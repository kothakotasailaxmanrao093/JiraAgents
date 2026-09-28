"""The whole routing sequence, end to end, without Temporal or Jira.

**The invariant: one user comment produces exactly one reply.** Every test here
counts the posts. A path that posts twice is as broken as one that posts nothing
after deciding to answer, and neither is visible from a live worker until a user
complains.

The fakes record every call, so "the model was never asked" and "nothing was
posted" are assertions rather than assumptions.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.router_flow import run_router
from shared.contract import AgentResult, CreatedIssue, EmailKind, Outcome


class Tools:
    """Records every activity call and replays a queued answer per tool."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args))
        answer = self.answers.get(name)
        if callable(answer):
            return answer(*args)
        return answer if answer is not None else {}

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def count(self, name: str) -> int:
        return self.names().count(name)

    def args(self, name: str) -> tuple:
        for called, args in self.calls:
            if called == name:
                return args
        raise AssertionError(f"{name} was never called")

    @property
    def posted_blocks(self) -> list[tuple[str, object]]:
        return list(self.args("post_reply")[1])

    def reply_text(self) -> str:
        out: list[str] = []
        for heading, body in self.posted_blocks:
            out.append(str(heading))
            if isinstance(body, list):
                out.extend(str(x) for x in body)
            else:
                out.append(str(body))
        return "\n".join(out)


class Children:
    """Stands in for agentExecutor. Records dispatches; can fail on demand."""

    def __init__(self, result: Any = None, raises: Exception | None = None) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[tuple[str, dict, dict]] = []

    async def dispatch(self, agent_name: str, payload: dict, **options: Any) -> Any:
        self.calls.append((agent_name, payload, options))
        if self.raises:
            raise self.raises
        return self.result


def gate(**over: Any) -> dict[str, Any]:
    base = {
        "outcome": "proceed",
        "task_queue_overrides": {},
        "run_id": "4a81f7c2",
        "issue_key": "FL-120",
        "comment_id": "10501",
        "comment_body": "@Aetherion build a tyre pressure check",
        "issue_summary": "Depot operations",
        "has_children": False,
        "idempotency_key": "comment_created:10501",
        "trigger_keyword": "Aetherion",
        "processed_label": "aetherion-processed",
        "existing_labels": [],
        "read_only": False,
    }
    base.update(over)
    return base


def created_contract(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        produced_by="JiraTaskCreation",
        display_name="JiraTaskCreation",
        agent_version="4.3.0",
        outcome=Outcome.CREATED,
        headline="Work breakdown created",
        summary="Created 1 Story and 3 Sub-tasks.",
        created=[CreatedIssue(key="FL-121", issue_type="Story", summary="Record a check")],
        sources_read=["FL-120 description"],
        email_recommended=True,
        email_kind=EmailKind.CREATED,
    )
    base.update(over)
    return AgentResult(**base).to_dict()


def reviewed_contract(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        produced_by="JiraRequirementReview",
        display_name="JiraRequirementReview",
        agent_version="2.2.1",
        outcome=Outcome.REVIEWED,
        headline="Requirement review — no gaps found",
        summary="Read 1 source(s).",
        sources_read=["FL-120 description"],
        email_recommended=False,
        email_kind=EmailKind.NONE,
    )
    base.update(over)
    return AgentResult(**base).to_dict()


PAYLOAD = {"issue_key": "FL-120", "comment_id": "10501", "webhookEvent": "comment_created"}


# --- exactly one reply, on every path ----------------------------------------


async def test_a_build_request_dispatches_once_and_posts_once() -> None:
    tools = Tools(ingress_check=gate(), post_reply={"posted": True, "comment_id": "10502"})
    children = Children(result=created_contract())

    result = await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert len(children.calls) == 1
    assert tools.count("post_reply") == 1
    assert result["posted"] is True
    assert result["created"] == ["FL-121"]


@pytest.mark.parametrize(
    "body, expect_dispatch",
    [
        ("@Aetherion build a thing", True),
        ("@Aetherion review this", True),
        ("@Aetherion help", False),
        # Bug 5: dispatched to Work Breakdown now, not answered generically by
        # the router — see test_classify.py::test_a_question_is_dispatched_to_work_breakdown.
        ("@Aetherion explain this", True),
    ],
)
async def test_every_answered_path_posts_exactly_one_reply(body, expect_dispatch) -> None:
    tools = Tools(ingress_check=gate(comment_body=body), post_reply={"posted": True})
    children = Children(result=created_contract())

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert tools.count("post_reply") == 1
    assert bool(children.calls) is expect_dispatch


async def test_a_dead_child_still_posts_exactly_one_reply() -> None:
    """The router owns the comment, so a dead child cannot mean silence."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    children = Children(raises=RuntimeError("workflow type not registered"))

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert tools.count("post_reply") == 1
    assert "did not respond" in tools.reply_text()


async def test_a_malformed_contract_still_posts_exactly_one_reply() -> None:
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    children = Children(result={"outcome": "NONSENSE"})

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert tools.count("post_reply") == 1
    assert "unreadable" in tools.reply_text()


# --- and no reply at all when the comment was never ours ---------------------


@pytest.mark.parametrize("reason", ["NO_KEYWORD", "SIGNATURE"])
async def test_an_ignored_comment_posts_nothing_and_dispatches_nothing(reason) -> None:
    """Silent by design. Answering "this wasn't for me" on every unrelated
    comment would make the system unbearable on a busy project."""
    tools = Tools(ingress_check={"outcome": "ignored", "ignored": reason, "run_id": "r1"})
    children = Children()

    result = await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert tools.count("post_reply") == 0
    assert children.calls == []
    assert result["status"] == "ignored"


async def test_a_redelivered_comment_is_not_answered_twice() -> None:
    """Jira redelivers. The whole point of gate 3."""
    tools = Tools(
        ingress_check={"outcome": "ignored", "ignored": "DUPLICATE", "run_id": "4a81f7c2"}
    )
    children = Children()

    result = await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert tools.count("post_reply") == 0
    assert children.calls == []
    assert result["ignored"] == "DUPLICATE"
    # The original run_id, so the redelivery traces to the run that answered it.
    assert result["run_id"] == "4a81f7c2"


async def test_the_duplicate_check_happens_before_any_model_call() -> None:
    """A redelivery must cost neither a model call nor a child workflow."""
    tools = Tools(ingress_check={"outcome": "ignored", "ignored": "DUPLICATE", "run_id": "r"})
    await run_router(PAYLOAD, tools.execute, Children().dispatch)

    assert "classify_intent" not in tools.names()


# --- the classifier is only asked when it is needed --------------------------


async def test_an_explicit_verb_never_calls_the_classifier() -> None:
    """Layer 1 is free. Paying for a model call on "build" would be waste on
    every single routed comment."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    await run_router(PAYLOAD, tools.execute, Children(result=created_contract()).dispatch)

    assert "classify_intent" not in tools.names()


async def test_a_comment_with_no_verb_asks_the_classifier() -> None:
    tools = Tools(
        ingress_check=gate(comment_body="@Aetherion this feels underspecified"),
        classify_intent={"scores": {"REVIEW": 0.88, "BUILD": 0.06}},
        post_reply={"posted": True},
    )
    children = Children(
        result=created_contract(
            produced_by="JiraRequirementReview",
            display_name="JiraRequirementReview",
            outcome=Outcome.REVIEWED,
            headline="Requirement review — nothing created",
            created=[],
            email_recommended=False,
            email_kind=EmailKind.NONE,
        )
    )

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert tools.count("classify_intent") == 1
    assert children.calls[0][0] == "JiraRequirementReview"


async def test_an_unreachable_classifier_asks_the_user_and_creates_nothing() -> None:
    tools = Tools(
        ingress_check=gate(comment_body="@Aetherion have a look at this"),
        classify_intent={"scores": None, "error": "gateway down"},
        post_reply={"posted": True},
    )
    children = Children()

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert children.calls == []
    assert tools.count("post_reply") == 1
    assert "Which did you mean?" in tools.reply_text()


async def test_a_close_call_between_build_and_review_dispatches_to_review() -> None:
    """Bug 5: this used to be the textbook ask-rather-than-guess example. It
    now dispatches to Requirement Review instead — reviewing creates nothing,
    so it is the reversible guess, and one round-trip is no longer spent
    asking when the two live options are BUILD and REVIEW specifically."""
    tools = Tools(
        ingress_check=gate(comment_body="@Aetherion have a look at this"),
        classify_intent={"scores": {"BUILD": 0.41, "REVIEW": 0.44}},
        post_reply={"posted": True},
    )
    children = Children(result=reviewed_contract())

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert children.calls[0][0] == "JiraRequirementReview"
    assert "build scored 0.41" in tools.reply_text()
    assert "Requirement Review" in tools.reply_text()


async def test_low_confidence_on_both_build_and_review_still_asks_the_user() -> None:
    """The BUILD/REVIEW tie-break needs a real floor (Bug 5) — two low scores
    must still be asked about, not guessed."""
    tools = Tools(
        ingress_check=gate(comment_body="@Aetherion have a look at this"),
        classify_intent={"scores": {"BUILD": 0.30, "REVIEW": 0.25}},
        post_reply={"posted": True},
    )
    children = Children()

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert children.calls == []
    assert "build 0.30" in tools.reply_text()


# --- what the child is told ---------------------------------------------------


async def test_the_child_is_forced_into_delegated_mode() -> None:
    """A child that posts its own reply would make two comments."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    children = Children(result=created_contract())

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    _, payload, _ = children.calls[0]
    assert payload["mode"] == "delegated"


async def test_the_review_child_is_additionally_forced_draft_only() -> None:
    tools = Tools(
        ingress_check=gate(comment_body="@Aetherion review this"), post_reply={"posted": True}
    )
    children = Children(result=created_contract(outcome=Outcome.REVIEWED, created=[]))

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    _, payload, _ = children.calls[0]
    assert payload["post_to_jira"] is False


async def test_the_child_workflow_id_is_derived_from_the_run_id() -> None:
    """Idempotency across the chain: a router replay cannot start the child twice."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    children = Children(result=created_contract())

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    _, _, options = children.calls[0]
    assert options["workflow_id"] == "build-4a81f7c2"
    assert options["execution_timeout"].total_seconds() > 0


# --- the run_id ties everything together -------------------------------------


async def test_the_run_id_ties_the_child_call_to_the_log_but_never_the_comment() -> None:
    """The run_id is the correlation id across logs/email/child call — Bug 7
    says it must never additionally leak into the rendered Jira comment."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    children = Children(result=created_contract())

    result = await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert result["run_id"] == "4a81f7c2"
    assert children.calls[0][1]["run_id"] == "4a81f7c2"
    assert tools.args("post_reply")[2] == "4a81f7c2"  # the post_reply activity arg
    assert "4a81f7c2" not in tools.reply_text()  # never the rendered comment


# --- attribution --------------------------------------------------------------


async def test_a_successful_run_names_the_child_that_ran() -> None:
    """The HUMAN display name, never the raw registered agent id (Bug 2)."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    await run_router(PAYLOAD, tools.execute, Children(result=created_contract()).dispatch)

    text = tools.reply_text()
    assert "Jira Orchestration → Work Breakdown" in text
    assert "JiraTaskCreation" not in text


async def test_a_failed_dispatch_names_no_child() -> None:
    """The router knows which agent it CHOSE. It must not claim one ran."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    children = Children(raises=RuntimeError("not deployed"))

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    handled_by = dict(tools.posted_blocks)["Handled by"]
    assert handled_by == "Jira Orchestration"  # no arrow, no child named

    text = tools.reply_text()
    assert "JiraTaskCreation" not in text  # the raw id must never leak in
    # …but the reply still says which agent was chosen, in "Routed to" — by
    # its human name, not the raw id.
    assert "Work Breakdown" in text


async def test_a_router_only_answer_names_no_child() -> None:
    tools = Tools(ingress_check=gate(comment_body="@Aetherion help"), post_reply={"posted": True})
    await run_router(PAYLOAD, tools.execute, Children().dispatch)

    assert "→" not in tools.reply_text()


# --- never email an invalid request -------------------------------------------


async def test_an_invalid_request_never_mentions_an_email() -> None:
    """The rule survives routing: the router refuses the recommendation outright
    for this outcome rather than trusting the child not to ask."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    children = Children(
        result=created_contract(
            outcome=Outcome.NOT_A_REQUIREMENT,
            headline="Invalid request — this is not a work requirement",
            created=[],
            # The child asks anyway. It must still be refused.
            email_recommended=True,
            email_kind=EmailKind.CREATED,
        )
    )

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert "Notification" not in tools.reply_text()


async def test_chatter_produces_no_email_and_no_dispatch() -> None:
    tools = Tools(
        ingress_check=gate(comment_body="@Aetherion who is the PM of India?"),
        classify_intent={"scores": {"CHATTER": 0.96}},
        post_reply={"posted": True},
    )
    children = Children()

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert children.calls == []
    assert "Notification" not in tools.reply_text()
    assert "not a work requirement" in tools.reply_text()


# --- when the reply itself cannot be posted -----------------------------------


async def test_a_failed_post_alerts_the_administrators() -> None:
    """The one failure the user cannot see, because the channel for telling them
    is the thing that broke."""
    tools = Tools(
        ingress_check=gate(),
        post_reply={"posted": False, "error": "403 Forbidden"},
    )

    await run_router(PAYLOAD, tools.execute, Children(result=created_contract()).dispatch)

    assert tools.count("notify_admins") == 1
    assert "403 Forbidden" in str(tools.args("notify_admins"))


async def test_read_only_mode_does_not_alert_anyone() -> None:
    """Not posting was the intention, not a failure."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": False, "read_only": True})

    await run_router(PAYLOAD, tools.execute, Children(result=created_contract()).dispatch)

    assert tools.count("notify_admins") == 0


# --- the task-queue fix: a real bug found on the first live dispatch --------


async def test_a_configured_task_queue_is_passed_to_dispatch() -> None:
    """Without this, Temporal defaults a child workflow to the CALLER's
    queue — the router's own — and the child fails immediately because that
    worker never registered its workflow type. This happened for real on the
    first live dispatch to JiraRequirementReview."""
    tools = Tools(
        ingress_check=gate(
            comment_body="@Aetherion review this",
            task_queue_overrides={"JiraRequirementReview": "abc123-task-queue"},
        ),
        post_reply={"posted": True},
    )
    children = Children(result=created_contract(outcome=Outcome.REVIEWED, created=[]))

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    _, _, options = children.calls[0]
    assert options["task_queue"] == "abc123-task-queue"


async def test_no_override_configured_means_no_task_queue_is_forced() -> None:
    """The common case: most deployments will not need this at all."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    children = Children(result=created_contract())

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    _, _, options = children.calls[0]
    assert "task_queue" not in options


async def test_the_override_is_looked_up_by_the_agent_actually_dispatched() -> None:
    """A queue configured for the wrong agent name must never leak across."""
    tools = Tools(
        ingress_check=gate(task_queue_overrides={"JiraRequirementReview": "wrong-agent-queue"}),
        post_reply={"posted": True},
    )
    children = Children(result=created_contract())  # BUILD route

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    _, _, options = children.calls[0]
    assert "task_queue" not in options  # JiraTaskCreation has no override here


# --- Bug 3: never flatten a dispatch failure ----------------------------------


async def test_a_dispatch_failure_names_the_real_error_in_problems() -> None:
    """ "did not respond" alone is not an acceptable failure message — the
    actual exception must reach the comment."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    children = Children(raises=TimeoutError("120s elapsed"))

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert "Problems" in dict(tools.posted_blocks)
    problems = dict(tools.posted_blocks)["Problems"]
    assert any("TimeoutError" in p and "120s elapsed" in p for p in problems)


@pytest.mark.parametrize(
    "alert, expected, forbidden",
    [
        # What notify_admins really returns today: it logs, it has no sender.
        (
            {"attempted": True, "sent": False},
            "Could not email the administrators — this was logged instead.",
            "Emailed the administrators.",
        ),
        ({"attempted": True, "sent": True}, "Emailed the administrators.", "Could not email"),
    ],
    ids=["logged only", "really sent"],
)
async def test_a_dispatch_failure_says_only_what_happened_to_the_alert(
    alert, expected, forbidden
) -> None:
    """D1a: "Emailed the administrators." was keyed on ``attempted`` and printed
    on every failure, although no email was ever sent."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": True}, notify_admins=alert)
    children = Children(raises=RuntimeError("workflow type not registered"))

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert tools.count("notify_admins") == 1
    assert expected in tools.reply_text()
    assert forbidden not in tools.reply_text()


async def test_a_dispatch_failure_with_no_admin_configured_has_no_notification_line() -> None:
    """notify_admins itself reports attempted=False when no ORCH_NOTIFY_EMAILS
    is configured; the reply must not claim an email went out that did not."""
    tools = Tools(
        ingress_check=gate(), post_reply={"posted": True}, notify_admins={"attempted": False}
    )
    children = Children(raises=RuntimeError("not deployed"))

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    assert "Notification" not in dict(tools.posted_blocks)


async def test_the_log_line_carries_the_run_id_and_the_real_error() -> None:
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    children = Children(raises=RuntimeError("workflow type not registered"))

    with caplog_workaround() as records:
        await run_router(PAYLOAD, tools.execute, children.dispatch)

    joined = "\\n".join(records)
    assert "run 4a81f7c2" in joined
    assert "RuntimeError" in joined
    assert "workflow type not registered" in joined


def caplog_workaround():
    """A tiny context manager collecting log records without pytest's caplog
    fixture, since this module's tests are plain async functions."""
    import logging

    class _Capture:
        def __init__(self):
            self.records: list[str] = []

        def __enter__(self):
            self.handler = logging.Handler()
            self.handler.emit = lambda record: self.records.append(record.getMessage())
            logging.getLogger("agent.router_flow").addHandler(self.handler)
            return self.records

        def __exit__(self, *exc):
            logging.getLogger("agent.router_flow").removeHandler(self.handler)

    return _Capture()


# --- B.6: a wrong agent name must not be retried for minutes -----------------


async def test_dispatch_uses_a_bounded_retry_policy() -> None:
    """The exact bug observed live: a child dispatched with no retry bound sat
    "Running" for 3m49s before failing, because Temporal kept retrying a
    workflow type that was never going to become registered. Every dispatch
    must now cap that wait to a few seconds."""
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    children = Children(result=created_contract())

    await run_router(PAYLOAD, tools.execute, children.dispatch)

    _, _, options = children.calls[0]
    policy = options["retry_policy"]
    assert policy.maximum_attempts > 0  # 0 means "unbounded" to Temporal
    assert policy.maximum_attempts <= 5  # bounded — not the 3m49s we watched


# --- B.2: the health check ----------------------------------------------------

SMTP_OK = Tools(smtp_login_check={"ok": True, "error": ""})


async def test_health_check_reports_the_smtp_login() -> None:
    """D2: an app password dies silently; the health check must say so."""
    from agent.router_flow import run_health_check

    dead = Tools(smtp_login_check={"ok": False, "error": "Gmail rejected the credentials (535)."})
    report = await run_health_check(Children(result={}).dispatch, dead.execute)

    assert report["smtp"] == {"ok": False, "error": "Gmail rejected the credentials (535)."}


async def test_a_raising_smtp_check_is_reported_not_raised() -> None:
    from agent.router_flow import run_health_check

    def _boom(*_a):
        raise TimeoutError("smtp unreachable")

    report = await run_health_check(
        Children(result={}).dispatch, Tools(smtp_login_check=_boom).execute
    )
    assert report["smtp"]["ok"] is False and "TimeoutError" in report["smtp"]["error"]


async def test_health_check_reports_every_catalog_agent() -> None:
    from agent.router_flow import run_health_check
    from routing.catalog import CATALOG

    children = Children(result={})
    report = await run_health_check(children.dispatch, SMTP_OK.execute)

    assert set(report["agents"]) == {spec.agent_name for spec in CATALOG}
    assert len(children.calls) == len(CATALOG)


async def test_health_check_reports_an_unreachable_agent_without_raising() -> None:
    from agent.router_flow import run_health_check

    children = Children(raises=RuntimeError("workflow type not registered"))
    report = await run_health_check(children.dispatch, SMTP_OK.execute)

    for outcome in report["agents"].values():
        assert outcome["reachable"] is False
        assert "workflow type not registered" in outcome["error"]


async def test_health_check_creates_nothing_and_posts_nothing() -> None:
    """It is diagnostic. Nothing it does may be visible to a real user."""
    from agent.router_flow import run_health_check

    children = Children(result=created_contract())
    await run_health_check(children.dispatch, SMTP_OK.execute)

    for _, payload, _ in children.calls:
        assert payload["issue_key"] == "FL"  # the known safe no-op, not a real ticket


async def test_health_check_is_reachable_through_the_agent_entry_point() -> None:
    from agent.router_agent import JiraOrchestration

    async def fake_dispatch(*a, **k):
        return created_contract()

    import agent.router_agent as router_agent_mod

    original = router_agent_mod._dispatch
    original_execute = router_agent_mod.toolExecutor.execute
    router_agent_mod._dispatch = fake_dispatch
    # No real SMTP login from a unit test.
    router_agent_mod.toolExecutor.execute = SMTP_OK.execute
    try:
        result = await JiraOrchestration.fn({"health_check": True})
    finally:
        router_agent_mod._dispatch = original
        router_agent_mod.toolExecutor.execute = original_execute

    assert result["status"] == "health_check"


# --- D1b: the router sends the one outcome email ----------------------------


@pytest.mark.parametrize(
    "outcome, kind",
    [
        (Outcome.CREATED, "created"),
        (Outcome.NEEDS_INFO, "clarification"),
        (Outcome.ALREADY_EXISTS, "duplicates"),
        (Outcome.FAILED, "failed"),
    ],
)
async def test_each_actionable_outcome_sends_exactly_one_email(outcome, kind) -> None:
    tools = Tools(
        ingress_check=gate(),
        post_reply={"posted": True},
        send_outcome_email={"attempted": True, "sent": True, "recipients": ["lead@example.com"]},
    )
    await run_router(
        PAYLOAD, tools.execute, Children(result=created_contract(outcome=outcome)).dispatch
    )

    assert tools.count("send_outcome_email") == 1
    assert tools.args("send_outcome_email")[0] == kind
    assert "Emailed lead@example.com." in tools.reply_text()


@pytest.mark.parametrize("outcome", [Outcome.NOT_A_REQUIREMENT, Outcome.REVIEWED])
async def test_an_invalid_request_or_a_review_never_emails(outcome) -> None:
    tools = Tools(ingress_check=gate(), post_reply={"posted": True})
    await run_router(
        PAYLOAD, tools.execute, Children(result=created_contract(outcome=outcome)).dispatch
    )

    assert tools.count("send_outcome_email") == 0
    assert "Notification" not in tools.reply_text()


@pytest.mark.parametrize(
    "emailed, expected",
    [
        (
            {"sent": False, "error": "Email not sent: GMAIL_APP_PASSWORD is not set."},
            "No email was sent — Email not sent: GMAIL_APP_PASSWORD is not set.",
        ),
        (
            {"sent": False, "suppressed_repeat": True},
            "Not emailed again — the same email was sent a few minutes ago.",
        ),
    ],
    ids=["failed", "repeat"],
)
async def test_an_email_that_did_not_go_out_is_never_called_emailed(emailed, expected) -> None:
    tools = Tools(ingress_check=gate(), post_reply={"posted": True}, send_outcome_email=emailed)
    await run_router(PAYLOAD, tools.execute, Children(result=created_contract()).dispatch)

    assert expected in tools.reply_text()
    assert "Emailed" not in tools.reply_text()


async def test_an_email_skipped_by_configuration_says_nothing() -> None:
    tools = Tools(
        ingress_check=gate(),
        post_reply={"posted": True},
        send_outcome_email={"sent": False, "skipped": "created is not in ORCH_NOTIFY_ON"},
    )
    await run_router(PAYLOAD, tools.execute, Children(result=created_contract()).dispatch)
    assert "Notification" not in tools.reply_text()
