"""Confluence comments and GitHub events pushed by webhooks, not polled (2026-10-14).

The webhook service (antennae unified-webhook) turns each delivery into flat
fields — confluence_page_id / confluence_comment_id, github_event / github_repo /
pr_number / … — and the platform hands the agent the ones its form declares.
Each pushed event goes through the very code the polls use, so the two never
answer or send the same thing twice.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fake_tools import Tools
from test_commands import PAGE, PR, SETTINGS, Agents, tools
from test_github_events import REPO, T0, FakeGitHub, Planning, pr, world  # noqa: F401

from agent import router_agent
from agent.confluence_flow import run_confluence_comment
from agent.router_flow import GITHUB_EVENT_FIELDS, run_github_event
from routing import github_events as gh
from tools import confluence_tools, github_tools

# --- the agent's entry point -----------------------------------------------------------


async def test_a_pushed_confluence_comment_is_answered_by_itself(monkeypatch) -> None:
    seen = []

    async def fake(execute, dispatch, page_id, comment_id):
        seen.append((page_id, comment_id))
        return {"status": "confluence_comment"}

    monkeypatch.setattr(router_agent, "run_confluence_comment", fake)
    out = await router_agent.JiraOrchestration.fn(
        {"confluence_page_id": "16908289", "confluence_comment_id": "16700001"}
    )
    assert out == {"status": "confluence_comment"}
    assert seen == [("16908289", "16700001")]


async def test_a_pushed_github_event_is_handled_by_itself(monkeypatch) -> None:
    seen = []

    async def fake(execute, dispatch, payload):
        seen.append(payload)
        return {"status": "github_event"}

    monkeypatch.setattr(router_agent, "run_github_event", fake)
    payload = {"github_event": "pull_request", "github_repo": REPO, "pr_number": 21}
    assert await router_agent.JiraOrchestration.fn(payload) == {"status": "github_event"}
    assert seen == [payload]


def test_the_form_declares_every_field_the_webhooks_send() -> None:
    """The platform fills an agent's input by field name: the names must match
    what the unified webhook's providers put on the event."""
    from pathlib import Path

    meta = json.loads((Path(__file__).resolve().parents[1] / "src/agent/metadata.json").read_text())
    names = {t["name"] for t in meta["config"]["triggers"]}
    assert {"confluence_page_id", "confluence_comment_id"} <= names
    assert set(GITHUB_EVENT_FIELDS) <= names


# --- the Confluence flow ---------------------------------------------------------------


def _found(event: dict | None, skipped: str = "") -> dict:
    return {
        "ok": True,
        "events": [event] if event else [],
        "pages": 1,
        "keyword": "Aetherion",
        "settings": SETTINGS,
        "skipped": skipped,
    }


async def test_the_pushed_comment_is_answered_under_it_like_a_polled_one() -> None:
    event = {"id": "c7", "root_id": "c7", "page_id": "9001", "text": "@Aetherion code confirm"}
    plan = {"proposed": {"source": "main", "branch": "f", "target": "main"}}
    t = tools(
        confluence_comment=_found(event),
        confluence_reply={"posted": True, "comment_id": "c8"},
        confluence_update_reply={"updated": True},
        read_tdd={**PAGE, "code_plan": plan},
    )
    out = await run_confluence_comment(t.execute, Agents().dispatch, "9001", "c7")

    assert t.args("confluence_comment") == ("9001", "c7")
    names = t.names()
    assert names.index("confluence_mark_answered") < names.index("confluence_reply"), "marked first"
    assert t.args("confluence_reply")[1] == "c7", "threaded under the comment"
    assert PR in str(t.args("story_comment")[1]), "the PR reaches the story too"
    assert out["answered"][0]["intent"] == "CODE" and out["skipped"] == ""


async def test_a_comment_that_is_not_for_the_agent_starts_nothing() -> None:
    t = tools(confluence_comment=_found(None, skipped="the agent's own reply"))
    out = await run_confluence_comment(t.execute, Agents().dispatch, "9001", "c9")
    assert t.names() == ["confluence_comment"]
    assert out == {
        "status": "confluence_comment",
        "comment": "c9",
        "answered": [],
        "skipped": "the agent's own reply",
    }


async def test_an_unreadable_comment_says_so() -> None:
    t = tools(confluence_comment={"ok": False, "error": "HTTPStatusError: 401"})
    out = await run_confluence_comment(t.execute, Agents().dispatch, "9001", "c9")
    assert out == {"status": "confluence_comment", "error": "HTTPStatusError: 401"}


# --- the Confluence tool, against a fake Confluence --------------------------------------


class FakeConfluence:
    def __init__(self) -> None:
        self.comments: dict[str, dict] = {}
        self.labels: dict[str, list[str]] = {"9001": ["aetherion-tdd"]}
        self.answered: dict[str, list[str]] = {}

    def comment(self, cid: str, text: str, page: str = "9001", parent: str = "") -> None:
        self.comments[cid] = {
            "id": cid,
            "type": "comment",
            "container": {"id": page, "title": "BTS-12 - TDD v1.2"},
            "ancestors": [{"id": parent, "type": "comment"}] if parent else [],
            "history": {"createdBy": {"displayName": "Priya"}, "createdDate": "2026-10-14"},
            "body": {"storage": {"value": f"<p>{text}</p>"}},
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")  # rest/api/content/<id>[/...]
        content_id = parts[3]
        if len(parts) == 4:
            found = self.comments.get(content_id)
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={})
        if parts[4] == "label":
            names = self.labels.get(content_id, [])
            return httpx.Response(200, json={"results": [{"name": n} for n in names]})
        if parts[4] == "property":
            ids = self.answered.get(content_id)
            if ids is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"value": {"ids": ids}})
        return httpx.Response(404, json={})


@pytest.fixture
def wiki(monkeypatch):
    fake = FakeConfluence()
    monkeypatch.setenv("ORCH_TRIGGER_KEYWORD", "Aetherion")
    monkeypatch.delenv("ORCH_TDD_LABEL", raising=False)
    monkeypatch.setattr(
        confluence_tools,
        "_wiki",
        lambda: httpx.AsyncClient(
            base_url="https://wiki.test", transport=httpx.MockTransport(fake.handle)
        ),
    )
    return fake


async def test_a_command_on_a_tdd_page_becomes_the_one_event(wiki) -> None:
    wiki.comment("c7", "@Aetherion explain why this approach")
    out = await confluence_tools.confluence_comment("9001", "c7")
    assert out["ok"] and out["skipped"] == ""
    [event] = out["events"]
    assert (event["id"], event["root_id"], event["page_id"]) == ("c7", "c7", "9001")
    assert event["text"] == "@Aetherion explain why this approach"
    assert event["page_title"] == "BTS-12 - TDD v1.2" and event["author"] == "Priya"
    assert out["keyword"] == "Aetherion" and "settings" in out


async def test_a_reply_is_answered_in_its_thread(wiki) -> None:
    wiki.comment("c8", "@Aetherion why not a counter?", parent="c7")
    [event] = (await confluence_tools.confluence_comment("9001", "c8"))["events"]
    assert event["root_id"] == "c7"


async def test_the_page_comes_from_the_comment_when_not_sent(wiki) -> None:
    wiki.comment("c7", "@Aetherion explain why")
    [event] = (await confluence_tools.confluence_comment("", "c7"))["events"]
    assert event["page_id"] == "9001"


@pytest.mark.parametrize(
    "setup,expected",
    [
        (lambda w: w.labels.update({"9001": []}), "not a TDD page"),
        (lambda w: w.answered.update({"9001": ["c7"]}), "already answered"),
        (lambda w: w.comment("c7", "AetherionAgent · About the design"), "the agent's own reply"),
        (lambda w: w.comment("c7", "looks good to me"), "does not start with @Aetherion"),
        (lambda w: w.comments.clear(), "the comment is gone"),
    ],
)
async def test_what_the_poll_would_skip_is_skipped(wiki, setup, expected) -> None:
    wiki.comment("c7", "@Aetherion explain why")
    setup(wiki)
    out = await confluence_tools.confluence_comment("9001", "c7")
    assert out["ok"] and out["events"] == [] and out["skipped"] == expected


# --- the GitHub tool, against the fake GitHub -------------------------------------------


class PushedGitHub(FakeGitHub):
    """The poll's fake GitHub, plus the single-item reads a pushed event makes."""

    def __init__(self) -> None:
        super().__init__()
        self.single: dict[str, dict] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in self.single:
            self.calls.append(path)
            return httpx.Response(200, json=self.single[path])
        parts = path.split("/")
        if len(parts) == 6 and parts[4] == "pulls" and parts[5].isdigit():
            self.calls.append(path)
            number = int(parts[5])
            found = [p for p in self.prs if p["number"] == number]
            return httpx.Response(200, json=found[0]) if found else httpx.Response(404, json={})
        return super().handle(request)


@pytest.fixture
def hub(world, monkeypatch):  # noqa: F811 — the fixture imported above
    _, jira, clock = world
    github = PushedGitHub()
    monkeypatch.setattr(
        github_tools,
        "_github",
        lambda: httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(github.handle)
        ),
    )
    return github, jira, clock


def fields(**over: Any) -> dict[str, Any]:
    base = {
        "github_event": "pull_request",
        "github_action": "closed",
        "github_repo": REPO,
        "pr_number": 21,
        "github_comment_id": None,
    }
    return {**base, **over}


def comment(cid: int, body: str, login: str = "meena", kind: str = "User") -> dict:
    return {
        "id": cid,
        "body": body,
        "user": {"login": login, "type": kind},
        "created_at": "2026-10-14T07:00:00Z",
        "submitted_at": "2026-10-14T07:00:00Z",
        "path": "src/main/java/com/bank/TransferService.java",
        "line": 40,
    }


async def test_a_pushed_merge_is_one_event_and_only_once(hub) -> None:
    github, jira, _ = hub
    github.prs = [pr(21, updated=T0, merged=T0)]
    github.files[21] = [{"filename": "src/A.java", "status": "modified"}]

    out = await github_tools.github_event(fields())
    [event] = out["events"]
    assert out["ok"] and out["skipped"] == ""
    assert event["kind"] == "pr_merged" and event["pr_number"] == 21
    assert event["key"] == gh.merge_key(REPO, github.prs[0])
    assert event["issue_key"] == "BGV-93", "the story named in the PR title"

    await github_tools.github_event_done(event["key"], True, "graph refreshed")
    again = await github_tools.github_event(fields())
    assert again["events"] == [] and again["skipped"] == "this merge was already sent"


@pytest.mark.parametrize(
    "change,over,expected",
    [
        (dict(), dict(github_action="opened"), "pull request opened is not a merge"),
        (dict(merged=None), dict(), "pull request closed is not a merge"),
        (dict(base="develop"), dict(), "merged into develop, not main"),
        (dict(), dict(github_repo="someone/else"), "someone/else is not a watched repository"),
        (dict(), dict(pr_number=None), "not about a pull request"),
    ],
)
async def test_what_is_not_a_merge_into_main_is_skipped(hub, change, over, expected) -> None:
    github, _, _ = hub
    github.prs = [pr(21, updated=T0, **{"merged": T0, **change})]
    out = await github_tools.github_event(fields(**over))
    assert out["events"] == [] and out["skipped"] == expected


@pytest.mark.parametrize(
    "event_name,kind,url",
    [
        ("issue_comment", "issue", f"/repos/{REPO}/issues/comments/901"),
        ("pull_request_review_comment", "line", f"/repos/{REPO}/pulls/comments/901"),
        ("pull_request_review", "review", f"/repos/{REPO}/pulls/21/reviews/901"),
    ],
)
async def test_a_pushed_comment_is_sent_alone(hub, event_name, kind, url) -> None:
    github, _, _ = hub
    github.prs = [pr(21, updated=T0)]
    github.single[url] = comment(901, "Please also log the count.")
    action = "submitted" if kind == "review" else "created"

    out = await github_tools.github_event(
        fields(github_event=event_name, github_action=action, github_comment_id="901")
    )
    [event] = out["events"]
    assert event["kind"] == "pr_comments"
    assert event["marks"] == [f"comment:{REPO}#21:{kind}-901"]
    assert "Please also log the count." in event["issue_text"]


async def test_a_bots_comment_is_skipped(hub) -> None:
    github, _, _ = hub
    github.prs = [pr(21, updated=T0)]
    github.single[f"/repos/{REPO}/issues/comments/902"] = comment(
        902, "Deployed preview", login="vercel[bot]", kind="Bot"
    )
    out = await github_tools.github_event(
        fields(github_event="issue_comment", github_action="created", github_comment_id="902")
    )
    assert out["events"] == [] and out["skipped"] == "a bot's, empty or already sent comment"


async def test_an_edited_comment_is_not_new(hub) -> None:
    github, _, _ = hub
    github.prs = [pr(21, updated=T0)]
    out = await github_tools.github_event(
        fields(github_event="issue_comment", github_action="edited", github_comment_id="903")
    )
    assert out["skipped"] == "a comment edited is not new"


async def test_an_event_still_waiting_is_retried_with_the_next_push(hub) -> None:
    github, jira, _ = hub
    github.prs = [pr(21, updated=T0, merged=T0)]
    first = await github_tools.github_event(fields())
    await github_tools.github_event_done(first["events"][0]["key"], False, "Planning down")

    github.prs.append(pr(22, updated=T0))
    later = await github_tools.github_event(fields(github_action="opened", pr_number=22))
    assert [e["key"] for e in later["events"]] == [first["events"][0]["key"]]
    assert later["events"][0]["tries"] == 1


# --- the GitHub flow ---------------------------------------------------------------------


EVENT = {
    "kind": "pr_merged",
    "key": f"merged:{REPO}#21:abc",
    "repo": REPO,
    "pr_number": 21,
    "pr_url": f"https://github.com/{REPO}/pull/21",
    "issue_text": "",
    "issue_key": "BTS-12",
    "workflow_id": "planning-gh-abc",
}


def _pushed(events: list[dict], skipped: str = "") -> dict:
    return {
        "ok": True,
        "events": events,
        "repos": 1,
        "notes": [],
        "planning_agent": "planning_agent",
        "planning_queue": "q",
        "repos_text": f"bank: {REPO}",
        "timeout_minutes": 15,
        "skipped": skipped,
    }


async def test_a_pushed_merge_refreshes_the_graph_and_is_announced() -> None:
    t = Tools(
        github_event=_pushed([EVENT]),
        github_event_done={"recorded": True, "gave_up": False},
    )
    planning = Planning({"status": "success", "mode": "graph_sync"})
    out = await run_github_event(t.execute, planning, {**fields(), "is_workflow_only": True})

    assert t.args("github_event") == (fields(),), "only the webhook's own fields"
    [(name, payload, _)] = planning.calls
    assert name == "planning_agent" and payload["issue_text"] == ""
    assert t.args("announce_merge")[:2] == ("BTS-12", 21)
    assert t.args("github_event_done") == (EVENT["key"], True, "graph refreshed")
    assert out["status"] == "github_event" and out["sent"] == 1 and out["skipped"] == ""


async def test_a_pushed_event_that_is_skipped_sends_nothing() -> None:
    t = Tools(github_event=_pushed([], skipped="pull request opened is not a merge"))
    planning = Planning({"status": "success"})
    out = await run_github_event(t.execute, planning, fields(github_action="opened"))
    assert planning.calls == []
    assert out["sent"] == 0 and out["skipped"] == "pull request opened is not a merge"


async def test_a_failed_read_starts_nothing() -> None:
    t = Tools(github_event={"ok": False, "error": "GitHub rejected the token (401)"})
    planning = Planning({"status": "success"})
    out = await run_github_event(t.execute, planning, fields())
    assert planning.calls == [] and out["error"] == "GitHub rejected the token (401)"
