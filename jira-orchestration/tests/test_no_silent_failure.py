"""D0: a run is either deliberately ignored, or it attempts exactly one reply.

Before this, ``ingress_check`` returned ``{"proceed": False}`` both for "not
for me" and for "Jira said 403", and the router dropped both silently. Six
more paths — a raising ingress, classifier, admin alert, outcome email or post
— crashed the workflow, which posts nothing either. Every case below that is
not in the ignored set would have ended with zero comments.

The guarantee, for every way a run can end:
    outcome is "ignored" with an IgnoreReason   → zero replies
    anything else                               → exactly one post_reply call
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from agent.router_flow import run_router
from routing.ingress import IgnoreReason
from shared.contract import AgentResult, Outcome
from tests.fake_tools import Tools
from tools import tools

PAYLOAD = {"issue_key": "BGV-50", "comment_id": "10900", "webhookEvent": "comment_created"}


def _proceed(body: str = "@Aetherion build — let recruiters download a report as a PDF"):
    return {
        "outcome": "proceed",
        "run_id": "r1",
        "issue_key": "BGV-50",
        "comment_id": "10900",
        "comment_body": body,
        "trigger_keyword": "Aetherion",
        "idempotency_key": "comment_created:10900",
        "processed_label": "aetherion-processed",
        "task_queue_overrides": {},
    }


def _raise(exc: Exception):
    def _fn(*_args: Any) -> Any:
        raise exc

    return _fn


def _created() -> dict[str, Any]:
    return AgentResult(
        produced_by="JiraTaskCreation",
        display_name="Work Breakdown",
        agent_version="t",
        run_id="r1",
        outcome=Outcome.CREATED,
        headline="Work breakdown created",
        summary="1 Story created.",
    ).to_dict()


async def _dispatch_ok(*_a: Any, **_k: Any) -> dict:
    return _created()


async def _dispatch_raises(*_a: Any, **_k: Any) -> dict:
    raise RuntimeError("workflow type not registered")


async def _dispatch_garbage(*_a: Any, **_k: Any) -> dict:
    return {"outcome": "NOT_AN_OUTCOME"}


POSTED = {"posted": True, "comment_id": "1"}

# (name, tool answers, dispatch). Every way a run can end.
REPLYING_PATHS = [
    (
        "ingress failed (403)",
        {
            "ingress_check": {
                "outcome": "failed",
                "run_id": "r1",
                "issue_key": "BGV-50",
                "comment_id": "10900",
                "error": "HTTPStatusError: 403",
                "problem": "Jira returned 403 Forbidden for BGV-50.",
            }
        },
        _dispatch_ok,
    ),
    (
        "ingress activity raised",
        {"ingress_check": _raise(TimeoutError("activity timed out"))},
        _dispatch_ok,
    ),
    ("ingress returned an unknown outcome", {"ingress_check": {"outcome": "maybe"}}, _dispatch_ok),
    ("help", {"ingress_check": _proceed("@Aetherion help")}, _dispatch_ok),
    (
        "chatter",
        {
            "ingress_check": _proceed("@Aetherion hello there"),
            "classify_intent": {"scores": {"BUILD": 0, "REVIEW": 0, "QUESTION": 0, "CHATTER": 1}},
        },
        _dispatch_ok,
    ),
    (
        "classifier raised",
        {
            "ingress_check": _proceed("@Aetherion hmm, thoughts?"),
            "classify_intent": _raise(TimeoutError("model timeout")),
        },
        _dispatch_ok,
    ),
    ("built", {"ingress_check": _proceed()}, _dispatch_ok),
    ("child unreachable", {"ingress_check": _proceed()}, _dispatch_raises),
    (
        "child unreachable, admin alert raised",
        {"ingress_check": _proceed(), "notify_admins": _raise(ConnectionError("smtp down"))},
        _dispatch_raises,
    ),
    ("child returned garbage", {"ingress_check": _proceed()}, _dispatch_garbage),
    (
        "outcome email raised",
        {"ingress_check": _proceed(), "send_outcome_email": _raise(ConnectionError("smtp down"))},
        _dispatch_ok,
    ),
    (
        "post_reply raised",
        {"ingress_check": _proceed(), "post_reply": _raise(TimeoutError("jira timeout"))},
        _dispatch_ok,
    ),
]


@pytest.mark.parametrize(
    "name,answers,dispatch", REPLYING_PATHS, ids=[p[0] for p in REPLYING_PATHS]
)
async def test_every_non_ignored_run_attempts_exactly_one_reply(name, answers, dispatch) -> None:
    fake = Tools(**{"post_reply": POSTED, **answers})
    result = await run_router(PAYLOAD, fake.execute, dispatch)
    assert fake.names().count("post_reply") == 1, f"{name}: {fake.names()}"
    assert result["status"] != "ignored"


@pytest.mark.parametrize("reason", list(IgnoreReason), ids=[r.name for r in IgnoreReason])
async def test_every_ignored_reason_stays_silent(reason: IgnoreReason) -> None:
    fake = Tools(ingress_check={"outcome": "ignored", "ignored": reason.name, "run_id": "r1"})
    result = await run_router(PAYLOAD, fake.execute, _dispatch_ok)
    assert fake.names() == ["ingress_check"]
    assert result["status"] == "ignored"


async def test_a_post_that_fails_alerts_the_administrators() -> None:
    fake = Tools(ingress_check=_proceed(), post_reply=_raise(TimeoutError("jira timeout")))
    await run_router(PAYLOAD, fake.execute, _dispatch_ok)
    assert "notify_admins" in fake.names()


# --- the reply itself ------------------------------------------------------


async def test_an_unreadable_ticket_gets_the_error_reply() -> None:
    blocks: list[Any] = []

    def _post(issue_key, reply, *rest):
        blocks.extend(reply)
        return POSTED

    fake = Tools(
        ingress_check={
            "outcome": "failed",
            "run_id": "r1",
            "issue_key": "BGV-50",
            "comment_id": "10900",
            "error": "HTTPStatusError: 403",
            "problem": "Jira returned 403 Forbidden for BGV-50. This usually means …",
        },
        notify_admins={"attempted": True, "sent": True},
        post_reply=_post,
    )
    result = await run_router(PAYLOAD, fake.execute, _dispatch_ok)
    text = "\n".join(f"{h}\n{b}" for h, b in blocks)
    assert blocks[0][0] == "AetherionAgent · Could not read this ticket — nothing was done"
    assert ("Handled by", "Jira Orchestration") in blocks
    assert "Not routed — the request could not be read" in text
    assert "could not read BGV-50. Nothing was created and nothing was changed" in text
    assert "403 Forbidden for BGV-50" in text
    assert ("Notification", "Emailed the administrators.") in blocks
    assert result["status"] == "failed"
    assert fake.names().index("notify_admins") < fake.names().index("post_reply")


# --- ingress_check, against a fake Jira ------------------------------------------


def _jira(handler):
    return lambda: httpx.AsyncClient(
        base_url="https://example.atlassian.net", transport=httpx.MockTransport(handler)
    )


async def test_a_403_from_jira_is_a_failure_not_an_ignore(monkeypatch) -> None:
    monkeypatch.setattr(tools, "_client", _jira(lambda r: httpx.Response(403, json={})))
    monkeypatch.delenv("ORCH_ALLOWED_PROJECT_KEYS", raising=False)
    gate = await tools.ingress_check("BGV-50", "10900")
    assert gate["outcome"] == "failed"
    assert "Browse Projects permission" in gate["problem"]
    assert "HTTPStatusError" in gate["error"]


@pytest.mark.parametrize(
    "code,phrase",
    [(404, "404 Not Found"), (401, "401 Unauthorized"), (502, "server error (502)")],
)
async def test_each_read_failure_is_explained(monkeypatch, code, phrase) -> None:
    monkeypatch.setattr(tools, "_client", _jira(lambda r: httpx.Response(code, text="x")))
    monkeypatch.delenv("ORCH_ALLOWED_PROJECT_KEYS", raising=False)
    gate = await tools.ingress_check("BGV-50", "10900")
    assert gate["outcome"] == "failed" and phrase in gate["problem"]


async def test_a_comment_without_the_keyword_is_ignored_not_failed(monkeypatch) -> None:
    issue = {
        "fields": {
            "summary": "x",
            "comment": {
                "comments": [{"id": "10900", "body": "just talking to a colleague", "author": {}}]
            },
        }
    }
    monkeypatch.setattr(tools, "_client", _jira(lambda r: httpx.Response(200, json=issue)))
    monkeypatch.delenv("ORCH_ALLOWED_PROJECT_KEYS", raising=False)
    gate = await tools.ingress_check("BGV-50", "10900")
    assert gate == {"outcome": "ignored", "ignored": "NO_KEYWORD", "run_id": gate["run_id"]}
