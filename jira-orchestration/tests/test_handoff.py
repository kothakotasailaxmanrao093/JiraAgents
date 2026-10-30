"""The story read once, the review's questions with their answers, the ranked
services, and each card's history (ORCH_HANDOFF, 2026-10-29; kept by the
Requirement Review agent since 2026-10-30).

    @Aetherion review       the review reads and keeps the card's memory; the
                            reply adds the ranked services and the answers
    @Aetherion create TDD   the story is read once; Review brings the memory up
                            to date; Planning gets the card, the ranked services
                            and the questions with their answers
    @Aetherion code         each pull request goes into the project's history
    a merge                 so do its branch and changed files

Review, Planning and Jira are faked to their contracts; with ORCH_HANDOFF off
nothing of this runs (the other test files run that way).
"""

from __future__ import annotations

from typing import Any

import httpx

from agent.command_flow import run_command
from agent.router_flow import run_github_event, run_router
from routing import github_events as gh
from routing import handoff
from shared.keywords import AGENT_FOOTER
from tests.fake_tools import Tools
from tests.test_commands import DOCUMENT, PUBLISHED, SETTINGS, STORY, coded, command, reviewed
from tests.test_orchestration import gate
from tests.test_router_flow import PAYLOAD, reviewed_contract
from tools import pipeline_tools

ON = {**SETTINGS, "memory": True, "handoff": True}
RANKED = [
    {
        "service": "bank",
        "repo": "AetherionAgentDemo/bank-transfer-service",
        "branch": "main",
        "rank": 9,
        "reason": "spec.pdf names POST /transfers",
        "prs": [
            {
                "card": "BGV-90",
                "pr": 41,
                "branch": "feature/BGV-90-limits",
                "url": "https://github.com/AetherionAgentDemo/bank-transfer-service/pull/41",
                "state": "merged",
            }
        ],
    },
]
QUESTIONS = [
    {"question": "Is the limit per account?", "answers": ["Yes, per account."]},
    {"question": "What is the reset time?", "answers": []},
]
MEMORY = {"services": RANKED, "questions": QUESTIONS}
BUNDLE = {
    "ok": True,
    "root": {
        "key": "BGV-105",
        "type": "Story",
        "summary": "Daily transfer limit",
        "description": "Limit 2,00,000 INR a day.",
    },
    "tickets": [{"key": "BGV-106", "summary": "Tests", "description": "Edge at midnight"}],
    "unread": [],
    "discussion": [{"who": "person", "author": "Ravi", "text": "Yes, per account."}],
    "comments": ["Yes, per account."],
    "people_comments": [{"id": "10520", "text": "Yes, per account."}],
    "files": [
        {
            "id": "1",
            "filename": "spec.pdf",
            "kind": "document",
            "text": "POST /transfers",
            "note": "",
            "url": "https://x/1",
            "mime": "",
        },
        {
            "id": "2",
            "filename": "error.png",
            "kind": "image",
            "text": "",
            "note": "image: compared by file, not by content",
            "url": "https://x/2",
            "mime": "",
        },
    ],
}
SEEN = [
    {"id": "1", "filename": "spec.pdf", "kind": "document", "text": "POST /transfers", "note": ""},
    {
        "id": "2",
        "filename": "error.png",
        "kind": "image",
        "text": "Error: limit reached",
        "note": "",
    },
]


class Agents:
    """Review and Planning faked to their contracts, by mode; every call recorded."""

    def __init__(self, memory_fails: bool = False, review_memory: dict | None = None):
        self.memory_fails = memory_fails
        self.review_memory = review_memory or {}
        self.calls: list[tuple[str, dict, dict]] = []

    async def dispatch(self, agent: str, payload: dict, **options: Any) -> Any:
        self.calls.append((agent, payload, options))
        if agent == "JiraRequirementReview":
            mode = payload.get("mode")
            if mode == "memory":
                if self.memory_fails:
                    return [{"status": "error", "message": "Memsy is not reachable"}]
                return [{"status": "success", "mode": "memory", **MEMORY}]
            if mode == "history":
                return [{"status": "success", "mode": "history", "actor": "work:BGV"}]
            asked = ["Is the limit per account?", "What is the reset time?"]
            return [{**reviewed(4, asked), "memory": self.review_memory}]
        if agent == "coding-agent":
            return coded()
        mode = payload.get("mode") or "plan"
        if mode == "facts":
            return {"status": "success", "verdict": "new", "facts": [], "facts_hash": "fh"}
        if mode == "record":
            return {"status": "success"}
        return {
            "status": "success",
            "results": [],
            "document": {
                **DOCUMENT,
                "decisions": [{"id": "per-account", "choice": "limit per account"}],
            },
        }

    def sent(self, agent: str, mode: str | None = None) -> list[dict]:
        return [p for a, p, _ in self.calls if a == agent and p.get("mode") == mode]

    def review(self, mode: str | None = None, n: int = 0) -> dict:
        return self.sent("JiraRequirementReview", mode)[n]

    def plan(self) -> dict:
        return self.sent("planning_agent")[0]


def handoff_tools(**over: Any) -> Tools:
    return Tools(
        story_context={**STORY, "tdd": {}},
        story_bundle=over.pop("story_bundle", BUNDLE),
        read_media={"ok": True, "files": SEEN},
        publish_tdd={**PUBLISHED, "tdd_version": "v1.0"},
        earlier_designs={"designs": []},
        read_tdd={"ok": False},
        remember_fingerprint={"ok": True},
        **over,
    )


# --- create TDD ---------------------------------------------------------------------------


async def test_the_story_is_read_once_for_the_whole_tdd() -> None:
    t, agents = handoff_tools(), Agents()
    await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    assert t.count("story_bundle") == 1 and t.count("read_media") == 1
    assert t.count("story_text") == 0 and t.count("planning_input") == 0


async def test_planning_gets_the_agreed_input() -> None:
    t, agents = handoff_tools(), Agents()
    await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    sent = agents.plan()
    assert sent["issue_text"] == "" and sent["return_document"] is True
    assert sent["card"]["key"] == "BGV-105" and sent["card"]["title"] == "Daily transfer limit"
    assert "Sub-task BGV-106: Tests" in sent["card"]["description"]
    assert sent["card"]["comments"] == ["Yes, per account."]
    # The screenshot was looked at, now that a design is really written.
    assert sent["card"]["files"] == [
        {"name": "spec.pdf", "content": "POST /transfers"},
        {"name": "error.png", "content": "Error: limit reached"},
    ]
    assert sent["services"] == RANKED
    assert sent["questions"] == QUESTIONS
    assert sent["repos"] == "bank: AetherionAgentDemo/bank-transfer-service"


async def test_the_readiness_review_reads_and_keeps_the_cards_memory() -> None:
    t, agents = handoff_tools(), Agents()
    await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    assert agents.review("delegated")["memory_repos"] == SETTINGS["repos"]


async def test_review_brings_the_memory_up_to_date_with_the_comments() -> None:
    t, agents = handoff_tools(), Agents()
    await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    asked = agents.review("memory")
    assert asked["issue_key"] == "BGV-105" and asked["title"] == "Daily transfer limit"
    assert asked["comments"] == [{"id": "10520", "text": "Yes, per account."}]
    assert "=== spec.pdf ===" in asked["card_text"] and asked["repos"] == SETTINGS["repos"]


async def test_the_reply_shows_the_ranked_services_and_the_answers() -> None:
    t, agents = handoff_tools(), Agents()
    out = await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    sections = dict(out.blocks)
    assert sections["Services designed, by rank"] == [
        "bank — 9/10: spec.pdf names POST /transfers (earlier: BGV-90 PR #41, merged)"
    ]
    assert sections["Answers the design follows"] == [
        "Is the limit per account? → Yes, per account."
    ]


async def test_the_tdd_goes_into_the_projects_history() -> None:
    t, agents = handoff_tools(), Agents()
    await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    kept = agents.review("history")
    assert kept["event"] == "design" and kept["event_id"] == "v1.0"
    assert kept["data"]["services"] == ["bank"]
    assert kept["data"]["decisions"] == ["limit per account"]


async def test_when_the_memory_cannot_be_refreshed_the_reviews_own_is_used() -> None:
    t, agents = handoff_tools(), Agents(memory_fails=True, review_memory=MEMORY)
    await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    assert agents.plan()["services"] == RANKED


async def test_without_the_memory_the_tdd_is_written_from_the_old_input() -> None:
    t, agents = handoff_tools(), Agents(memory_fails=True)
    out = await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    sent = agents.plan()
    assert "card" not in sent and "Requirement BGV-105: Daily transfer limit" in sent["issue_text"]
    assert agents.sent("JiraRequirementReview", "history") == []
    assert "Services designed, by rank" not in dict(out.blocks)


async def test_an_unreadable_story_falls_back_to_the_old_reads() -> None:
    t = handoff_tools(
        story_bundle={"ok": False, "error": "500"},
        story_text={"ok": True, "text": "BGV-105", "hash": "h"},
        planning_input={"issue_text": "Requirement BGV-105: Daily transfer limit"},
    )
    agents = Agents()
    await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    assert t.count("story_text") == 1 and t.count("planning_input") == 1
    assert "card" not in agents.plan()


async def test_the_memory_check_compares_pictures_by_file_not_by_their_reading() -> None:
    text = handoff.memory_text({**BUNDLE, "files": SEEN})
    assert "POST /transfers" in text and "Error: limit reached" not in text
    assert "error.png (file 2)" in text


# --- code ------------------------------------------------------------------------------


async def test_each_pull_request_goes_into_the_projects_history() -> None:
    page = {
        "ok": True,
        "page_id": "9001",
        "title": "BGV-105 - TDD v1.0",
        "url": "u",
        "text": "1. Overview",
        "issue_key": "BGV-105",
        "code_plan": {"proposed": {"source": "main", "branch": "feature/x", "target": "main"}},
        "discussion": [],
    }
    t = Tools(
        story_context={**STORY, "tdd": {"page_id": "9001"}},
        read_tdd=page,
        branch_defaults={"repo": "o/r", "source": "main", "branch": "feature/x", "target": "main"},
        save_code_plan={"saved": True},
        planning_input={"issue_text": "Requirement"},
    )
    agents = Agents()
    await run_command(command("CODE", "@Aetherion code confirm", t, agents, settings=ON))
    kept = agents.review("history")
    assert kept["event"] == "pr_opened" and kept["event_id"] == "2"
    assert kept["data"] | {} == {
        "title": "BGV-105 - TDD v1.0",
        "repo": "AetherionAgentDemo/bank-transfer-service",
        "pr": 2,
        "url": "https://github.com/AetherionAgentDemo/bank-transfer-service/pull/2",
        "branch": "feature/BGV-105-daily-transfer-limit",
        "base": "main",
        "service": "bank",
        "tests_passed": True,
    }


# --- review ----------------------------------------------------------------------------


def _review_gate(commands: dict[str, Any] | None = None) -> dict[str, Any]:
    return gate(
        "@Aetherion review",
        issue_key="FL-120",
        commands=commands or {**ON, "repos": "bank: AetherionAgentDemo/bank-transfer-service"},
    )


async def test_a_review_reads_its_memory_and_shows_the_services_and_answers() -> None:
    t = Tools(ingress_check=_review_gate())
    sent: list[tuple[str, dict]] = []

    async def dispatch(agent, payload, **options):
        sent.append((agent, payload))
        return [reviewed_contract(questions=["What is the reset time?"], memory=MEMORY)]

    await run_router(PAYLOAD, t.execute, dispatch)
    ((agent, payload),) = sent
    assert agent == "JiraRequirementReview"
    assert payload["memory_repos"] == "bank: AetherionAgentDemo/bank-transfer-service"
    reply = t.reply_text()
    assert "Services this card touches (0-10)" in reply
    assert "bank — 9/10: spec.pdf names POST /transfers (earlier: BGV-90 PR #41, merged)" in reply
    assert "Is the limit per account? → Yes, per account." in reply
    assert t.count("story_bundle") == 0


async def test_a_review_without_memory_has_no_memory_sections() -> None:
    t = Tools(ingress_check=_review_gate())

    async def dispatch(agent, payload, **options):
        return [reviewed_contract()]

    await run_router(PAYLOAD, t.execute, dispatch)
    reply = t.reply_text()
    assert "Requirement review — no gaps found" in reply
    assert "Services this card touches" not in reply


async def test_without_handoff_a_review_is_not_asked_for_memory() -> None:
    t = Tools(ingress_check=_review_gate({**SETTINGS, "memory": True}))
    sent: list[dict] = []

    async def dispatch(agent, payload, **options):
        sent.append(payload)
        return [reviewed_contract()]

    await run_router(PAYLOAD, t.execute, dispatch)
    assert len(sent) == 1 and "memory_repos" not in sent[0]


# --- merge --------------------------------------------------------------------------------


async def test_a_merge_goes_into_the_projects_history_with_its_files() -> None:
    event = {
        "kind": "pr_merged",
        "key": "merged:o/r#2:x",
        "repo": "o/r",
        "pr_number": 2,
        "pr_url": "https://github.com/o/r/pull/2",
        "issue_text": "",
        "issue_key": "BGV-105",
        "workflow_id": "w",
        "tries": 0,
        "branch": "feature/BGV-105-limit",
        "files": ["bank/src/TransferService.java"],
    }
    scan = {
        "ok": True,
        "events": [event],
        "repos": 1,
        "notes": [],
        "planning_agent": "planning_agent",
        "planning_queue": "",
        "timeout_minutes": 15,
        "repos_text": "bank: o/r",
        "handoff": True,
        "review_queue": "review-queue",
    }
    t = Tools(
        github_event=scan, github_event_done={"recorded": True}, announce_merge={"jira": True}
    )
    agents = Agents()
    await run_github_event(
        t.execute,
        agents.dispatch,
        {"github_event": "pull_request", "github_repo": "o/r", "pr_number": 2},
    )
    kept = agents.review("history")
    assert kept["event"] == "pr_state" and kept["event_id"] == "2-merged"
    assert kept["data"] == {
        "repo": "o/r",
        "pr": 2,
        "url": "https://github.com/o/r/pull/2",
        "state": "merged",
        "branch": "feature/BGV-105-limit",
        "files": ["bank/src/TransferService.java"],
    }
    ((options,),) = [(o,) for a, p, o in agents.calls if a == "JiraRequirementReview"]
    assert options["task_queue"] == "review-queue"


def test_a_merge_event_carries_its_branch_and_files() -> None:
    pr = {
        "number": 2,
        "html_url": "u",
        "title": "BGV-105 limit",
        "head": {"ref": "feature/BGV-105-limit"},
        "base": {"ref": "main"},
    }
    event = gh.merged_event("o/r", pr, [{"filename": "a.java"}, {"filename": "b.java"}], [])
    assert event.branch == "feature/BGV-105-limit" and event.files == ["a.java", "b.java"]


# --- the one read ---------------------------------------------------------------------------


def _jira(listed: list[dict], comments: list[dict]) -> httpx.AsyncClient:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/rest/api/3/issue/BGV-105":
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "summary": "Daily limit",
                        "issuetype": {"name": "Story"},
                        "description": None,
                        "attachment": listed,
                        "comment": {"comments": comments},
                    }
                },
            )
        if "/attachment/content/" in request.url.path:
            return httpx.Response(200, content=b"remark,expected\nrent,ok\n")
        return httpx.Response(404)

    client = httpx.AsyncClient(
        base_url="https://x.atlassian.net", transport=httpx.MockTransport(handler)
    )
    client.seen = seen  # type: ignore[attr-defined]
    return client


def _adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


async def test_the_one_read_lists_once_and_keeps_peoples_comments_with_ids(monkeypatch) -> None:
    listed = [
        {
            "id": "1",
            "filename": "cases.csv",
            "created": "1",
            "content": "https://x.atlassian.net/rest/api/3/attachment/content/1",
        },
        {
            "id": "2",
            "filename": "error.png",
            "created": "2",
            "content": "https://x.atlassian.net/rest/api/3/attachment/content/2",
        },
    ]
    comments = [
        {"id": "10520", "author": {"displayName": "Ravi"}, "body": _adf("Yes, per account.")},
        {
            "id": "10521",
            "author": {"displayName": "Bot"},
            "body": _adf(f"AetherionAgent · Requirement review\n{AGENT_FOOTER}"),
        },
    ]
    client = _jira(listed, comments)
    monkeypatch.setattr(pipeline_tools, "_client", lambda: client)
    out = await pipeline_tools.story_bundle("BGV-105", [])
    assert out["ok"] and out["people_comments"] == [{"id": "10520", "text": "Yes, per account."}]
    assert client.seen.count("/rest/api/3/issue/BGV-105") == 1
    by_name = {f["filename"]: f for f in out["files"]}
    assert "rent | ok" in by_name["cases.csv"]["text"]
    assert by_name["error.png"]["text"] == "" and by_name["error.png"]["url"].endswith("/2")


async def test_read_media_reads_only_the_pictures_and_drops_the_links(monkeypatch) -> None:
    from shared import attachments

    async def look(data, filename, mime_type="", **_):
        return attachments.Extracted(filename=filename, text=f"read {filename}")

    monkeypatch.setattr(attachments, "extract", look)
    monkeypatch.setattr(pipeline_tools, "_client", lambda: _jira([], []))
    files = [
        {**f, "url": f"https://x.atlassian.net/rest/api/3/attachment/content/{f['id']}"}
        for f in BUNDLE["files"]
    ]
    out = await pipeline_tools.read_media("BGV-105", "Daily limit", files)
    by_name = {f["filename"]: f for f in out["files"]}
    assert by_name["spec.pdf"]["text"] == "POST /transfers"  # not read again
    assert by_name["error.png"]["text"] == "read error.png"
    assert all("url" not in f and "mime" not in f for f in out["files"])


# --- pure rules ---------------------------------------------------------------------------


def test_only_the_ranked_services_repositories_are_synced() -> None:
    repos = "bank: o/bank\ncards: o/cards@dev\nledger = o/ledger"
    assert handoff.ranked_repos(repos, [{"service": "cards"}]) == "cards: o/cards@dev"
    assert handoff.ranked_repos(repos, [{"service": "ledger"}]) == "ledger = o/ledger"
    assert handoff.ranked_repos(repos, []) == repos
    assert handoff.ranked_repos(repos, [{"service": "gone"}]) == repos


def test_sections_go_before_the_footer_and_empty_ones_are_left_out() -> None:
    blocks = [("AetherionAgent · Review", ""), ("What happened", "x"), ("", AGENT_FOOTER)]
    out = handoff.with_sections(blocks, [("Services", ["a"]), ("Answered so far", [])])
    assert out == [blocks[0], blocks[1], ("Services", ["a"]), blocks[2]]


def test_a_pull_request_link_gives_its_repository_and_number() -> None:
    ((event_id, data),) = handoff.pr_events(coded(), "TDD")
    assert event_id == "2" and data["repo"] == "AetherionAgentDemo/bank-transfer-service"
    assert handoff.pr_events({"pull_requests": [{"pr_url": "not a link"}]}, "TDD") == []


async def test_planning_is_named_once_among_who_handled_it() -> None:
    t, agents = handoff_tools(), Agents()
    out = await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    assert out.ran.count("Planning") == 1
