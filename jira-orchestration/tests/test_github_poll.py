"""GitHub → Planning: merges into main and new PR comments, each given once (2026-10-03).

The tools run for real against a fake GitHub and a fake Jira (the memory lives
in Jira project properties), at the HTTP level.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fake_tools import Tools

from agent.router_flow import run_github_poll
from routing import github_events as gh
from tools import github_tools

REPO = "AetherionAgentDemo/FirstRepo"
T0 = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def pr(
    number: int, *, updated: datetime, merged: datetime | None = None, base: str = "main", **over
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
    for name in ("ORCH_GITHUB_REPOS", "ORCH_GITHUB_BRANCH", "ORCH_GITHUB_MAX_TRIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        github_tools,
        "_github",
        lambda: httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(github.handle)
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


async def scan(clock, at: datetime) -> dict:
    clock["now"] = at
    return await github_tools.github_scan()


# --- the first check ---------------------------------------------------------------


async def test_the_first_check_watches_from_now_and_sends_no_history(world) -> None:
    github, jira, clock = world
    github.prs = [pr(1, updated=T0 - timedelta(days=3), merged=T0 - timedelta(days=3))]
    result = await scan(clock, T0)
    assert result["ok"] and result["events"] == []
    assert f"{REPO}: watching from now on" in result["notes"]
    assert jira.properties["aetherion-github-poll"]["repos"][REPO]["cursor"] == T0.isoformat()


# --- a merge into main ---------------------------------------------------------------


async def test_a_merge_into_main_is_given_once_with_its_files_and_comments(world) -> None:
    github, jira, clock = world
    await scan(clock, T0)
    merged_at = T0 + timedelta(seconds=20)
    github.prs = [pr(2, updated=merged_at, merged=merged_at)]
    github.files[2] = [
        {"filename": "src/reminders.py", "status": "added", "additions": 40, "deletions": 0}
    ]
    github.line_comments[2] = [
        {
            "id": 7,
            "user": {"login": "arjun"},
            "body": "Send at 10:00 IST",
            "created_at": iso(T0),
            "path": "src/reminders.py",
            "line": 12,
        }
    ]
    github.issue_comments[2] = [
        {
            "id": 8,
            "user": {"login": "dependabot[bot]", "type": "Bot"},
            "body": "bump",
            "created_at": iso(T0),
        }
    ]
    github.change()

    [event] = (await scan(clock, T0 + timedelta(minutes=1)))["events"]
    assert event["kind"] == "pr_merged" and event["pr_number"] == 2
    assert event["issue_key"] == "BGV-93"
    text = event["issue_text"]
    assert text.startswith(f"A pull request was merged into main in {REPO}")
    assert "PR #2: BGV-93: Change 2" in text and "Adds referee reminders." in text
    assert "- src/reminders.py (added, +40 −0)" in text
    assert "- arjun on src/reminders.py line 12: Send at 10:00 IST" in text
    assert "bump" not in text, "bot comments are left out"
    assert len(jira.pending()) == 1, "stored before it is handed out"

    assert (await github_tools.github_event_done(event["key"], True, "plan generated"))["recorded"]
    assert jira.pending() == {}
    github.change()  # re-read inside the overlap: already sent, so not again
    assert (await scan(clock, T0 + timedelta(minutes=2)))["events"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"merged": None, "state": "closed"},  # closed without merging
        {"base": "develop"},  # merged into another branch
    ],
)
async def test_what_is_not_a_merge_into_main_is_not_given(world, change) -> None:
    github, jira, clock = world
    await scan(clock, T0)
    at = T0 + timedelta(seconds=30)
    github.prs = [pr(3, updated=at, merged=change.pop("merged", at), **change)]
    github.change()
    assert (await scan(clock, T0 + timedelta(minutes=1)))["events"] == []


async def test_an_unchanged_repo_costs_one_free_call(world) -> None:
    github, jira, clock = world
    await scan(clock, T0)
    github.calls.clear()
    jira.properties["aetherion-github-poll"]["repos"][REPO]["etag"] = github.etag
    result = await scan(clock, T0 + timedelta(minutes=1))
    assert result["events"] == []
    assert github.calls == ["/orgs/AetherionAgentDemo/repos", f"/repos/{REPO}/pulls"]


# --- comments --------------------------------------------------------------------------


async def test_only_new_comments_are_given_and_each_once(world) -> None:
    github, jira, clock = world
    await scan(clock, T0)
    at = T0 + timedelta(seconds=40)
    github.prs = [pr(4, updated=at, title="Consent page")]
    github.issue_comments[4] = [
        {
            "id": 1,
            "user": {"login": "old"},
            "body": "From yesterday",
            "created_at": iso(T0 - timedelta(days=1)),
        },
        {
            "id": 2,
            "user": {"login": "arjun"},
            "body": "Why a new table here?",
            "created_at": iso(at),
        },
    ]
    github.change()
    [event] = (await scan(clock, T0 + timedelta(minutes=1)))["events"]
    assert event["kind"] == "pr_comments" and event["issue_key"] == ""
    assert "New comments on a pull request (open)" in event["issue_text"]
    assert "- arjun: Why a new table here?" in event["issue_text"]
    assert "From yesterday" not in event["issue_text"]
    await github_tools.github_event_done(event["key"], True)

    github.issue_comments[4].append(
        {
            "id": 3,
            "user": {"login": "priya"},
            "body": "To keep history.",
            "created_at": iso(T0 + timedelta(minutes=1, seconds=10)),
        }
    )
    github.change()
    [again] = (await scan(clock, T0 + timedelta(minutes=2)))["events"]
    assert "- priya: To keep history." in again["issue_text"]
    assert "Why a new table here?" not in again["issue_text"], "already given"


# --- failures --------------------------------------------------------------------------


async def test_a_failed_planning_run_is_retried_then_given_up(world, monkeypatch) -> None:
    github, jira, clock = world
    monkeypatch.setenv("ORCH_GITHUB_MAX_TRIES", "3")
    await scan(clock, T0)
    at = T0 + timedelta(seconds=10)
    github.prs = [pr(5, updated=at, merged=at)]
    github.change()
    [event] = (await scan(clock, T0 + timedelta(minutes=1)))["events"]

    assert (await github_tools.github_event_done(event["key"], False, "Neo4j unreachable"))[
        "gave_up"
    ] is False
    [retry] = (await scan(clock, T0 + timedelta(minutes=2)))["events"]
    assert retry["key"] == event["key"] and retry["tries"] == 1
    assert (await github_tools.github_event_done(event["key"], False, "again"))["gave_up"] is False
    assert (await github_tools.github_event_done(event["key"], False, "again"))["gave_up"] is True
    assert jira.pending() == {}
    assert (await scan(clock, T0 + timedelta(minutes=3)))["events"] == []


async def test_a_check_that_dies_half_way_loses_nothing(world) -> None:
    github, jira, clock = world
    await scan(clock, T0)
    at = T0 + timedelta(seconds=10)
    github.prs = [pr(6, updated=at, merged=at)]
    github.change()
    [event] = (await scan(clock, T0 + timedelta(minutes=1)))["events"]
    # The check stopped before Planning answered: nothing was recorded.
    [again] = (await scan(clock, T0 + timedelta(minutes=5)))["events"]
    assert again["key"] == event["key"] and again["tries"] == 0
    assert len(jira.pending()) == 1, "kept once, not duplicated"


async def test_an_unconfigured_check_says_what_is_missing(monkeypatch) -> None:
    for name in (
        "GITHUB_TOKEN",
        "ORCH_GITHUB_ORG",
        "ORCH_GITHUB_STATE_PROJECT",
        "ORCH_ALLOWED_PROJECT_KEYS",
    ):
        monkeypatch.delenv(name, raising=False)
    result = await github_tools.github_scan()
    assert not result["ok"]
    assert "GITHUB_TOKEN" in result["error"] and "ORCH_GITHUB_ORG" in result["error"]


async def test_archived_and_unlisted_repos_are_skipped(world, monkeypatch) -> None:
    github, jira, clock = world
    github.repos += [
        {"name": "Old", "full_name": "AetherionAgentDemo/Old", "archived": True},
        {"name": "Other", "full_name": "AetherionAgentDemo/Other", "archived": False},
    ]
    monkeypatch.setenv("ORCH_GITHUB_REPOS", "firstrepo")
    assert (await scan(clock, T0))["repos"] == 1


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


# --- the poll run ----------------------------------------------------------------------


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
SCAN = {
    "ok": True,
    "events": [EVENT],
    "repos": 1,
    "notes": [],
    "planning_agent": "planning_agent",
    "planning_queue": "local-planning-agent-q",
    "timeout_minutes": 15,
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


async def test_each_event_is_given_to_planning_as_its_issue_text() -> None:
    tools = Tools(github_scan=SCAN, github_event_done={"recorded": True, "gave_up": False})
    planning = Planning({"status": "success", "tdd": {}})
    result = await run_github_poll(tools.execute, planning)

    [(name, payload, options)] = planning.calls
    assert name == "planning_agent"
    assert payload["issue_text"] == EVENT["issue_text"]
    assert payload["event"] == "pr_merged" and payload["issue_key"] == "BGV-93"
    assert options["task_queue"] == "local-planning-agent-q"
    assert options["workflow_id"] == "planning-gh-abc"
    assert tools.args("github_event_done") == (EVENT["key"], True, "plan generated")
    assert result["sent"] == 1 and result["failed"] == 0


@pytest.mark.parametrize(
    "answer,expected",
    [
        ({"status": "error", "message": "Neo4j unreachable"}, "Neo4j unreachable"),
        (RuntimeError("worker gone"), "RuntimeError: worker gone"),
    ],
)
async def test_a_failed_planning_run_is_recorded_for_a_retry(answer, expected) -> None:
    tools = Tools(github_scan=SCAN, github_event_done={"recorded": True, "gave_up": False})
    result = await run_github_poll(tools.execute, Planning(answer))
    assert tools.args("github_event_done") == (EVENT["key"], False, expected)
    assert result["failed"] == 1
    assert "notify_admins" not in tools.names()


async def test_the_admins_hear_when_retries_run_out() -> None:
    tools = Tools(github_scan=SCAN, github_event_done={"recorded": True, "gave_up": True})
    await run_github_poll(tools.execute, Planning({"status": "error", "message": "down"}))
    subject = tools.args("notify_admins")[0]
    assert subject == f"PR #2 in {REPO} could not be given to Planning"


async def test_a_failed_scan_starts_nothing() -> None:
    tools = Tools(github_scan={"ok": False, "error": "GitHub rejected the token (401)"})
    planning = Planning({"status": "success"})
    result = await run_github_poll(tools.execute, planning)
    assert planning.calls == []
    assert result["error"] == "GitHub rejected the token (401)"


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
