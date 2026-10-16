"""GitHub → Planning: merges into main and new PR comments, each given once (2026-10-03).

Since 2026-10-16 every GitHub event arrives by webhook (no polling). The tools
run for real against a fake GitHub and a fake Jira (the memory lives in Jira
project properties), at the HTTP level; the fixtures here are shared with
test_pushed_events.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fake_tools import Tools

from agent.router_flow import run_github_event
from routing import github_events as gh
from tools import github_tools

REPO = "AetherionAgentDemo/FirstRepo"
T0 = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def pr(
    number: int,
    *,
    updated: datetime,
    merged: datetime | None = None,
    base: str = "main",
    **over,
):
    return {
        "number": number,
        "title": over.pop("title", f"BGV-93: Change {number}"),
        "body": over.pop("body", "Adds referee reminders."),
        "state": over.pop("state", "closed" if merged else "open"),
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "user": {"login": "meena", "type": "User"},
        "head": {"ref": f"feature/change-{number}"},
        "base": {"ref": base},
        "merged_at": iso(merged) if merged else None,
        "merge_commit_sha": f"9f3a1c2{number:04d}" if merged else None,
        "updated_at": iso(updated),
        **over,
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.repos = [{"name": "FirstRepo", "full_name": REPO, "archived": False}]
        self.prs: list[dict] = []
        self.files: dict[int, list[dict]] = {}
        self.issue_comments: dict[int, list[dict]] = {}
        self.line_comments: dict[int, list[dict]] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.etag = 'W/"v1"'
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        since = gh.parse_time(query.get("since"))

        def after(items: list[dict]) -> list[dict]:
            return [c for c in items if not since or gh.parse_time(c["created_at"]) >= since]

        if path == "/orgs/AetherionAgentDemo/repos":
            return httpx.Response(200, json=self.repos)
        if path == f"/repos/{REPO}/pulls":
            if request.headers.get("If-None-Match") == self.etag:
                return httpx.Response(304)
            ordered = sorted(self.prs, key=lambda p: p["updated_at"], reverse=True)
            return httpx.Response(200, json=ordered, headers={"ETag": self.etag})
        number = int(path.split("/")[5])
        if path.endswith("/files"):
            return httpx.Response(200, json=self.files.get(number, []))
        if "/issues/" in path:
            return httpx.Response(200, json=after(self.issue_comments.get(number, [])))
        if path.endswith("/comments"):
            return httpx.Response(200, json=after(self.line_comments.get(number, [])))
        if path.endswith("/reviews"):
            return httpx.Response(200, json=self.reviews.get(number, []))
        return httpx.Response(404, json={"message": "Not Found"})

    def change(self) -> None:
        """Something changed: GitHub hands out a new ETag."""
        self.etag = f'W/"v{len(self.calls)}"'


class FakeJira:
    def __init__(self) -> None:
        self.properties: dict[str, Any] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET":
            if name not in self.properties:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"key": name, "value": self.properties[name]})
        if request.method == "PUT":
            self.properties[name] = json.loads(request.content)
            return httpx.Response(200, json={})
        if request.method == "DELETE":
            return httpx.Response(204 if self.properties.pop(name, None) is not None else 404)
        return httpx.Response(405)

    def pending(self) -> dict[str, Any]:
        return {k: v for k, v in self.properties.items() if k.startswith("aetherion-gh-")}


@pytest.fixture
def world(monkeypatch):
    github, jira = FakeGitHub(), FakeJira()
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    monkeypatch.setenv("ORCH_GITHUB_ORG", "AetherionAgentDemo")
    monkeypatch.setenv("ORCH_GITHUB_STATE_PROJECT", "BGV")
    monkeypatch.setenv("ORCH_ALLOWED_PROJECT_KEYS", "BGV,FL")
    # The developer's own .env is loaded on import; none of it may reach these tests.
    for name in (
        "ORCH_GITHUB_REPOS",
        "ORCH_GITHUB_BRANCH",
        "ORCH_GITHUB_MAX_TRIES",
        "ORCH_PLANNING_REPOS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        github_tools,
        "_github",
        lambda: httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(github.handle),
        ),
    )
    monkeypatch.setattr(
        github_tools,
        "_client",
        lambda: httpx.AsyncClient(
            base_url="https://jira.test", transport=httpx.MockTransport(jira.handle)
        ),
    )
    clock = {"now": T0}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(github_tools, "datetime", Clock)
    return github, jira, clock


# --- the text --------------------------------------------------------------------------


def test_a_very_long_conversation_is_shortened_to_fit_jira() -> None:
    comments = [
        gh.Comment(f"issue-{i}", "x", "word " * 300, f"2026-10-03T10:{i % 60:02d}:00Z")
        for i in range(60)
    ]
    text = gh.merged_text(REPO, pr(9, updated=T0, merged=T0, body="b" * 9000), [], comments)
    assert len(text) <= gh.MAX_TEXT
    assert "(the 30 earliest are not shown)" in text


def test_comments_on_a_merged_pr_say_it_is_merged() -> None:
    merged = pr(10, updated=T0, merged=T0 - timedelta(hours=1))
    text = gh.comments_text(REPO, merged, [gh.Comment("issue-1", "a", "late note", iso(T0))])
    assert "(already merged)" in text


# --- the GitHub event run ----------------------------------------------------------------------


EVENT = {
    "kind": "pr_merged",
    "key": f"merged:{REPO}#2:abc",
    "repo": REPO,
    "pr_number": 2,
    "pr_url": f"https://github.com/{REPO}/pull/2",
    "issue_text": "A pull request was merged…",
    "issue_key": "BGV-93",
    "workflow_id": "planning-gh-abc",
    "tries": 0,
}
PUSHED = {
    "ok": True,
    "events": [EVENT],
    "repos": 1,
    "notes": [],
    "planning_agent": "planning_agent",
    "planning_queue": "local-planning-agent-q",
    "timeout_minutes": 15,
}


# The webhook's fields for a merge of PR #2 (what the platform puts in the input).
MERGED = {
    "github_event": "pull_request",
    "github_action": "closed",
    "github_repo": REPO,
    "pr_number": 2,
    "github_comment_id": None,
}


class Planning:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict, dict]] = []

    async def __call__(self, name: str, payload: dict, **options: Any) -> Any:
        self.calls.append((name, payload, options))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def test_a_merge_refreshes_that_repositorys_graph_and_writes_no_design() -> None:
    """2026-10-08: the merge brings the graph up to date for the next design."""
    scan = {**PUSHED, "repos_text": f"demo: {REPO}\nuser: org/user"}
    tools = Tools(github_event=scan, github_event_done={"recorded": True, "gave_up": False})
    planning = Planning({"status": "success", "mode": "graph_sync", "results": []})
    result = await run_github_event(tools.execute, planning, MERGED)

    [(name, payload, options)] = planning.calls
    assert name == "planning_agent"
    assert payload["issue_text"] == "", "no requirement: Planning only syncs the graph"
    assert payload["repos"] == f"demo: {REPO}", "that repository, under its configured name"
    assert payload["event"] == "pr_merged" and payload["issue_key"] == "BGV-93"
    assert options["task_queue"] == "local-planning-agent-q"
    assert options["workflow_id"] == "planning-gh-abc"
    assert tools.args("github_event_done") == (EVENT["key"], True, "graph refreshed")
    assert result["sent"] == 1 and result["failed"] == 0


async def test_an_unconfigured_repository_is_named_after_itself() -> None:
    tools = Tools(github_event=PUSHED, github_event_done={"recorded": True, "gave_up": False})
    planning = Planning({"status": "success"})
    await run_github_event(tools.execute, planning, MERGED)
    assert planning.calls[0][1]["repos"] == f"{REPO.split('/')[1]}: {REPO}"


async def test_new_pr_comments_are_designed_for_against_every_repository() -> None:
    event = {
        **EVENT,
        "kind": "pr_comments",
        "issue_text": "New comments on a pull request…",
    }
    scan = {**PUSHED, "events": [event], "repos_text": f"demo: {REPO}\nuser: org/user"}
    tools = Tools(github_event=scan, github_event_done={"recorded": True, "gave_up": False})
    planning = Planning({"status": "success"})
    await run_github_event(tools.execute, planning, MERGED)

    payload = planning.calls[0][1]
    assert payload["issue_text"] == "New comments on a pull request…"
    assert payload["repos"] == f"demo: {REPO}\nuser: org/user"
    assert tools.args("github_event_done") == (event["key"], True, "plan generated")


@pytest.mark.parametrize(
    "answer,expected",
    [
        ({"status": "error", "message": "Neo4j unreachable"}, "Neo4j unreachable"),
        (RuntimeError("worker gone"), "RuntimeError: worker gone"),
    ],
)
async def test_a_failed_planning_run_is_recorded_for_a_retry(answer, expected) -> None:
    tools = Tools(github_event=PUSHED, github_event_done={"recorded": True, "gave_up": False})
    result = await run_github_event(tools.execute, Planning(answer), MERGED)
    assert tools.args("github_event_done") == (EVENT["key"], False, expected)
    assert result["failed"] == 1
    assert "notify_admins" not in tools.names()


async def test_the_admins_hear_when_retries_run_out() -> None:
    tools = Tools(github_event=PUSHED, github_event_done={"recorded": True, "gave_up": True})
    await run_github_event(tools.execute, Planning({"status": "error", "message": "down"}), MERGED)
    subject = tools.args("notify_admins")[0]
    assert subject == f"PR #2 in {REPO} could not be given to Planning"


@pytest.mark.parametrize(
    "title,branch,projects,expected",
    [
        ("BGV-93: Referee reminders", "feature/x", ("BGV",), "BGV-93"),
        ("Referee reminders", "feature/bgv-93-reminders", ("BGV",), "BGV-93"),
        ("Change 4", "feature/change-4", ("BGV",), ""),  # not a project of ours
        ("Change 4", "feature/change-4", (), ""),  # lower case, no project list
        ("FL-7 fix", "main", (), "FL-7"),
    ],
)
def test_the_jira_key_a_pr_names(title, branch, projects, expected) -> None:
    assert gh.jira_key({"title": title, "head": {"ref": branch}}, projects) == expected


# --- the tools, without a schedule ------------------------------------------------------


async def test_an_unconfigured_github_says_what_is_missing(monkeypatch) -> None:
    for name in (
        "GITHUB_TOKEN",
        "ORCH_GITHUB_ORG",
        "ORCH_GITHUB_STATE_PROJECT",
        "ORCH_ALLOWED_PROJECT_KEYS",
        "ORCH_PLANNING_REPOS",
    ):
        monkeypatch.delenv(name, raising=False)
    result = await github_tools.github_event(MERGED)
    assert not result["ok"]
    assert "GITHUB_TOKEN" in result["error"] and "ORCH_GITHUB_ORG" in result["error"]
