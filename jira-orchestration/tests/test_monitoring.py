"""What happened on a ticket, and an alert when a person is needed (2026-10-15)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fake_tools import Tools
from test_commands import PAGE, SETTINGS, Agents, coded, command, tools

from agent.command_flow import run_command
from agent.router_flow import run_router
from routing.classify import classify_by_verb
from routing.compose import status_blocks
from tools import tools as router_tools

HISTORY = {
    "active": {"intent": "BUILD", "run_id": "r9", "started": "2026-10-15T10:00:00+00:00"},
    "history": [
        {
            "at": "2026-10-15T09:00:00+00:00",
            "step": "REVIEW",
            "status": "finished",
            "detail": "Ready (5/5)",
            "run_id": "r1",
        },
        {
            "at": "2026-10-15T09:10:00+00:00",
            "step": "TDD",
            "status": "BTS-15 - TDD v1.0 ready",
            "detail": "https://wiki/p/1",
            "run_id": "r2",
        },
    ],
}


def text(blocks: list[tuple[str, object]]) -> str:
    out = []
    for heading, body in blocks:
        out.append(str(heading))
        out.extend(body if isinstance(body, list) else [str(body)])
    return "\n".join(out)


# --- "@Aetherion status" -----------------------------------------------------------------


@pytest.mark.parametrize(
    "comment", ["@Aetherion status", "@Aetherion what is the status?", "@Aetherion progress"]
)
def test_status_is_understood_without_a_model(comment) -> None:
    decision = classify_by_verb(comment)
    assert decision is not None and decision.intent == "STATUS" and decision.layer == 1


def test_the_status_reply_shows_what_runs_and_what_happened() -> None:
    reply = text(status_blocks("BTS-15", HISTORY))
    assert "Status of BTS-15" in reply
    assert "BUILD — started 2026-10-15T10:00:00+00:00 (run r9)" in reply
    assert "2026-10-15T09:00:00+00:00 — REVIEW finished: Ready (5/5) (run r1)" in reply
    assert "TDD BTS-15 - TDD v1.0 ready: https://wiki/p/1" in reply


def test_a_new_ticket_says_nothing_has_happened() -> None:
    reply = text(status_blocks("BTS-99", {}))
    assert "Nothing is running on this ticket." in reply
    assert "Nothing has been asked of the agents on this ticket yet." in reply


async def test_the_router_answers_status_from_the_tickets_history() -> None:
    gate = {
        "outcome": "proceed",
        "run_id": "r10",
        "issue_key": "BTS-15",
        "comment_id": "c1",
        "comment_body": "@Aetherion status",
        "trigger_keyword": "Aetherion",
    }
    t = Tools(ingress_check=gate, ticket_state={"state": HISTORY})
    dispatched: list = []

    async def dispatch(*a, **k):
        dispatched.append(a)

    out = await run_router({"issue_key": "BTS-15", "comment_id": "c1"}, t.execute, dispatch)
    assert out["intent"] == "STATUS" and out["posted"] is True
    assert dispatched == [], "no agent is called to answer it"
    assert "REVIEW finished: Ready (5/5)" in t.reply_text()


# --- the history itself, against a fake Jira ------------------------------------------------


class FakeJira:
    def __init__(self) -> None:
        self.properties: dict[str, Any] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if "/properties/" in request.url.path:
            if request.method == "GET":
                if name not in self.properties:
                    return httpx.Response(404, json={})
                return httpx.Response(200, json={"value": self.properties[name]})
            self.properties[name] = json.loads(request.content)
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})  # labels and the like


@pytest.fixture
def jira(monkeypatch):
    fake = FakeJira()
    monkeypatch.setattr(
        router_tools,
        "_client",
        lambda: httpx.AsyncClient(base_url="https://j", transport=httpx.MockTransport(fake.handle)),
    )
    monkeypatch.setenv("ORCH_STATUS_LABELS", "false")
    return fake


async def test_a_job_and_any_step_land_in_the_history(jira) -> None:
    await router_tools.claim_ticket("bts-15", "REVIEW", "r1", "reply-1")
    await router_tools.finish_ticket(
        "BTS-15", "REVIEW", "r1", {"outcome": "reviewed", "headline": "Ready (5/5)"}, "fp"
    )
    await router_tools.record_step("BTS-15", "CODE", "Pull request raised", "https://pr/22", "r3")
    state = next(iter(jira.properties.values()))
    steps = [(s["step"], s["status"], s["detail"]) for s in state["history"]]
    assert steps == [
        ("REVIEW", "started", ""),
        ("REVIEW", "finished", "Ready (5/5)"),
        ("CODE", "Pull request raised", "https://pr/22"),
    ]
    assert state["active"] is None


async def test_the_history_keeps_the_latest_fifty(jira) -> None:
    for n in range(55):
        await router_tools.record_step("BTS-15", "TDD", f"v1.{n}", "", f"r{n}")
    history = next(iter(jira.properties.values()))["history"]
    assert len(history) == 50 and history[-1]["status"] == "v1.54"


async def test_no_story_no_history(jira) -> None:
    assert await router_tools.record_step("", "CODE", "x", "", "r") == {"recorded": False}


# --- commands are recorded; coding giving up raises an alert ----------------------------------


async def test_a_command_is_recorded_once_with_its_link() -> None:
    plan = {"proposed": {"source": "main", "branch": "f", "target": "main"}}
    t, agents = tools(read_tdd={**PAGE, "code_plan": plan}), Agents()
    await run_command(command("CODE", "@Aetherion code confirm", t, agents, page_id="9001"))
    issue_key, step, status, link, _ = t.args("record_step")
    assert (issue_key, step, status) == ("BGV-105", "CODE", "Pull request raised")
    assert link.startswith("https://github.com/")
    assert "notify_admins" not in t.names()


async def test_coding_that_gave_up_alerts_the_administrators() -> None:
    plan = {"proposed": {"source": "main", "branch": "f", "target": "main"}}
    failing = coded()
    failing["pull_requests"][0]["tests_passed"] = False
    t = tools(read_tdd={**PAGE, "code_plan": plan})
    agents = Agents(**{"coding-agent": [failing]})
    await run_command(
        command(
            "CODE",
            "@Aetherion code confirm",
            t,
            agents,
            page_id="9001",
            settings={**SETTINGS, "coding_attempts": 1},
        )
    )
    subject, body, _ = t.args("notify_admins")
    assert subject == "Coding gave up on BGV-105"
    assert "tests FAILED" in body and "A person is needed" in body


# --- the alert channel works with email off ----------------------------------------------------


async def test_alerts_reach_the_chat_channel_even_with_email_off(monkeypatch) -> None:
    posted = []

    def handle(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={})

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr(router_tools.httpx, "AsyncClient", client)
    monkeypatch.setenv("ORCH_NOTIFY_ON", "none")
    monkeypatch.setenv("ORCH_ALERT_WEBHOOK_URL", "https://hooks.example/alerts")

    out = await router_tools.notify_admins("Coding gave up on BTS-15", "tests FAILED", "r7")

    assert out["alerted"] is True and out["sent"] is False
    assert posted == [
        {"text": "[Aetherion] ALERT — Coding gave up on BTS-15\ntests FAILED\n(run r7)"}
    ]


async def test_no_channel_configured_is_as_before(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_NOTIFY_ON", "none")
    monkeypatch.delenv("ORCH_ALERT_WEBHOOK_URL", raising=False)
    out = await router_tools.notify_admins("x", "y", "r")
    assert "alerted" not in out and out["skipped"].startswith("email is off")


async def test_a_merge_is_recorded_in_the_storys_history() -> None:
    from agent.router_flow import run_github_event

    event = {
        "kind": "pr_merged",
        "key": "merged:o/bank#22:abc",
        "repo": "o/bank",
        "pr_number": 22,
        "pr_url": "https://github.com/o/bank/pull/22",
        "issue_text": "",
        "issue_key": "BTS-15",
        "workflow_id": "w",
    }
    scan = {
        "ok": True,
        "events": [event],
        "repos": 1,
        "notes": [],
        "planning_agent": "planning_agent",
        "planning_queue": "q",
        "repos_text": "bank: o/bank",
        "timeout_minutes": 15,
    }
    t = Tools(github_event=scan, github_event_done={"recorded": True, "gave_up": False})

    async def planning(name, payload, **options):
        return {"status": "success", "mode": "graph_sync"}

    await run_github_event(
        t.execute,
        planning,
        {"github_event": "pull_request", "github_repo": "o/bank", "pr_number": 22},
    )
    issue_key, step, status, link, _ = t.args("record_step")
    assert (issue_key, step, link) == ("BTS-15", "MERGE", "https://github.com/o/bank/pull/22")
    assert status == "o/bank #22 merged — Planning's graph refreshed"


async def test_a_review_shown_again_and_a_paused_build_are_in_the_history() -> None:
    """Live 2026-10-15: a reused review left "status" saying nothing had happened."""
    from test_orchestration import PAYLOAD, Children, Clock, gate, state

    t = Tools(ingress_check=gate("@Aetherion review", state(review_score=4)))
    await run_router(PAYLOAD, t.execute, Children().dispatch, Clock().sleep)
    assert t.args("record_step")[1:3] == ("REVIEW", "shown again — the ticket is unchanged")

    t = Tools(ingress_check=gate("@Aetherion build", state(review_score=2)))
    await run_router(PAYLOAD, t.execute, Children().dispatch, Clock().sleep)
    assert t.args("record_step")[1:3] == ("BUILD", "paused — readiness 2/5")
