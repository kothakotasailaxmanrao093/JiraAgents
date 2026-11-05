"""The card's memory, kept by Orchestration alone (ORCH_HANDOFF, 2026-10-30).

    @Aetherion review       Orchestration recalls the card (answers since,
                            earlier cards, decisions, ranked services) and gives
                            it to the review; after it, keeps its questions; the
                            reply adds the services, the answers and what is open
    @Aetherion create TDD   the story is read once; the readiness review is given
                            the memory; Planning gets the card, the ranked
                            services and the questions with their answers, and
                            every repository
    @Aetherion code         each pull request goes into the project's history
    a merge                 so do its branch and changed files

The memory tools, Review, Planning and Jira are faked to their contracts; with
ORCH_HANDOFF off nothing of this runs (the other test files run that way).
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
        "rank": 1,
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
    {"id": "Q1", "question": "Is the limit per account?", "answers": ["Yes, per account."]},
    {"id": "Q2", "question": "What is the reset time?", "answers": []},
]
KEPT = {"ok": True, "services": RANKED, "questions": QUESTIONS}
DOC = {"source_id": "card_memory", "source_label": "Earlier answers and related work"}
RECALLED = {"ok": True, "memory": {"seq": 2, "services": RANKED}, "document": DOC}
DOWN = {"ok": False, "error": "MemoryUnavailable: not set"}
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
    """Review, Planning and Coding faked to their contracts; every call recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []

    async def dispatch(self, agent: str, payload: dict, **options: Any) -> Any:
        self.calls.append((agent, payload, options))
        if agent == "JiraRequirementReview":
            return [reviewed(4, ["Is the limit per account?", "What is the reset time?"])]
        if agent == "coding-agent":
            return coded()
        if payload.get("mode") == "facts":
            return {"status": "success", "verdict": "new", "facts": [], "facts_hash": "fh"}
        if payload.get("mode") == "record":
            return {"status": "success", "stored": {"snapshot": {}}}
        return {
            "status": "success",
            "results": [],
            "document": {
                **DOCUMENT,
                "decisions": [{"id": "per-account", "choice": "limit per account"}],
            },
        }

    def sent(self, agent: str) -> list[dict]:
        return [p for a, p, _ in self.calls if a == agent]

    def plan(self) -> dict:
        return [p for p in self.sent("planning_agent") if not p.get("mode")][0]


def handoff_tools(**over: Any) -> Tools:
    return Tools(
        story_context={**STORY, "tdd": {}},
        story_bundle=over.pop("story_bundle", BUNDLE),
        read_media={"ok": True, "files": SEEN},
        publish_tdd={**PUBLISHED, "tdd_version": "v1.0"},
        earlier_designs={"designs": []},
        read_tdd={"ok": False},
        remember_fingerprint={"ok": True},
        recall_card=over.pop("recall_card", RECALLED),
        keep_card=over.pop("keep_card", KEPT),
        refresh_card=over.pop("refresh_card", KEPT),
        write_history={"ok": True},
        tdd_memory={"ok": True, "seq": 0, "snapshot": {}},
        keep_tdd_memory={"ok": True},
        decisions_in_force={"ok": True, "decisions": []},
        **over,
    )


# --- create TDD ---------------------------------------------------------------------------


async def test_the_story_is_read_once_for_the_whole_tdd() -> None:
    t, agents = handoff_tools(), Agents()
    await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    assert t.count("story_bundle") == 1 and t.count("read_media") == 1
    assert t.count("story_text") == 0 and t.count("planning_input") == 0


async def test_the_readiness_review_is_given_the_memory_and_its_questions_are_kept() -> None:
    t, agents = handoff_tools(), Agents()
    await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    key, title, text, comments, repos = t.args("recall_card")
    assert (key, title, repos) == ("BGV-105", "Daily transfer limit", SETTINGS["repos"])
    assert "=== spec.pdf ===" in text
    assert comments == [{"id": "10520", "text": "Yes, per account."}]
    assert agents.sent("JiraRequirementReview")[0]["memory_document"] == DOC
    _, memory, asked, *_ = t.args("keep_card")
    assert memory == RECALLED["memory"]
    assert asked == ["Is the limit per account?", "What is the reset time?"]
    assert t.count("refresh_card") == 0  # the review just brought it up to date


async def test_planning_gets_the_agreed_input_and_every_repository() -> None:
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
    assert sent["questions"] == [
        {"question": "Is the limit per account?", "answers": ["Yes, per account."]},
        {"question": "What is the reset time?", "answers": []},
    ]
    # The ranking orders the design; Planning's code search may add a service.
    assert sent["repos"] == SETTINGS["repos"]
    assert t.args("decisions_in_force") == ("BGV-105", ["bank"])


async def test_a_reused_review_brings_the_memory_up_to_date_for_the_tdd() -> None:
    t, agents = handoff_tools(), Agents()
    await run_command(
        command("TDD", "@Aetherion create TDD", t, agents, settings=ON, reusable_review=reviewed(4))
    )
    assert t.count("recall_card") == 0 and t.count("keep_card") == 0
    key, title, _, comments, _ = t.args("refresh_card")
    assert (key, title) == ("BGV-105", "Daily transfer limit")
    assert agents.plan()["services"] == RANKED


async def test_the_reply_shows_the_ranked_services_and_the_answers() -> None:
    t, agents = handoff_tools(), Agents()
    out = await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    sections = dict(out.blocks)
    assert sections["Services designed, in order (1 = the main one)"] == [
        "1. bank: spec.pdf names POST /transfers (earlier: BGV-90 PR #41, merged)"
    ]
    assert sections["Answers the design follows"] == [
        "Is the limit per account? → Yes, per account."
    ]


async def test_the_tdd_goes_into_the_projects_history_without_its_decisions() -> None:
    t, agents = handoff_tools(), Agents()
    await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    key, event, data, event_id = t.args("write_history")
    assert (key, event, event_id) == ("BGV-105", "design", "v1.0")
    assert data["services"] == ["bank"] and "decisions" not in data


async def test_without_the_memory_the_tdd_is_written_from_the_old_input() -> None:
    t = handoff_tools(recall_card=DOWN, refresh_card=DOWN)
    agents = Agents()
    out = await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    sent = agents.plan()
    assert "card" not in sent and "Requirement BGV-105: Daily transfer limit" in sent["issue_text"]
    assert "memory_document" not in agents.sent("JiraRequirementReview")[0]
    assert t.count("keep_card") == 0 and t.count("write_history") == 0
    assert "Services designed, in order (1 = the main one)" not in dict(out.blocks)


async def test_an_unreadable_story_falls_back_to_the_old_reads() -> None:
    t = handoff_tools(
        story_bundle={"ok": False, "error": "500"},
        story_text={"ok": True, "text": "BGV-105", "hash": "h"},
        planning_input={"issue_text": "Requirement BGV-105: Daily transfer limit"},
    )
    agents = Agents()
    await run_command(command("TDD", "@Aetherion create TDD", t, agents, settings=ON))
    assert t.count("story_text") == 1 and t.count("planning_input") == 1
    assert "card" not in agents.plan() and t.count("recall_card") == 0


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
        write_history={"ok": True},
    )
    await run_command(command("CODE", "@Aetherion code confirm", t, Agents(), settings=ON))
    key, event, data, event_id = t.args("write_history")
    assert (key, event, event_id) == ("BGV-105", "pr_opened", "2")
    assert data == {
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


def _review_tools(**over: Any) -> Tools:
    return Tools(
        ingress_check=_review_gate(over.pop("commands", None)),
        story_bundle={**BUNDLE, "root": {**BUNDLE["root"], "key": "FL-120"}},
        recall_card=over.pop("recall_card", RECALLED),
        keep_card=over.pop("keep_card", KEPT),
        **over,
    )


async def test_a_review_is_given_the_memory_and_its_questions_are_kept() -> None:
    t = _review_tools()
    sent: list[tuple[str, dict]] = []

    async def dispatch(agent, payload, **options):
        sent.append((agent, payload))
        return [reviewed_contract(questions=["What is the reset time?"], summary="Ready 4/5.")]

    await run_router(PAYLOAD, t.execute, dispatch)
    ((agent, payload),) = sent  # no other agent is asked for the memory
    assert agent == "JiraRequirementReview" and payload["memory_document"] == DOC
    key, _, _, comments, repos = t.args("recall_card")
    assert key == "FL-120" and repos == "bank: AetherionAgentDemo/bank-transfer-service"
    assert comments == [{"id": "10520", "text": "Yes, per account."}]
    assert t.args("keep_card") == (
        "FL-120",
        RECALLED["memory"],
        ["What is the reset time?"],
        "Daily transfer limit",
        "Ready 4/5.",
    )
    reply = t.reply_text()
    assert "Services this card touches (1 = the main one)" in reply
    assert "1. bank: spec.pdf names POST /transfers (earlier: BGV-90 PR #41, merged)" in reply
    assert "Q1 Is the limit per account? → Yes, per account." in reply
    assert 'Still open — answer in a comment starting "Q<number>:"' in reply
    assert "Q2 What is the reset time?" in reply


async def test_a_review_stands_when_the_memory_is_down() -> None:
    t = _review_tools(recall_card=DOWN)
    sent: list[dict] = []

    async def dispatch(agent, payload, **options):
        sent.append(payload)
        return [reviewed_contract()]

    await run_router(PAYLOAD, t.execute, dispatch)
    assert "memory_document" not in sent[0] and t.count("keep_card") == 0
    reply = t.reply_text()
    assert "Requirement review — no gaps found" in reply
    assert "Services this card touches" not in reply


async def test_without_handoff_a_review_reads_no_memory() -> None:
    t = _review_tools(commands={**SETTINGS, "memory": True})
    sent: list[dict] = []

    async def dispatch(agent, payload, **options):
        sent.append(payload)
        return [reviewed_contract()]

    await run_router(PAYLOAD, t.execute, dispatch)
    assert "memory_document" not in sent[0]
    assert t.count("story_bundle") == 0 and t.count("recall_card") == 0


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
    }
    t = Tools(
        github_event=scan,
        github_event_done={"recorded": True},
        announce_merge={"jira": True},
        write_history={"ok": True},
    )
    agents = Agents()
    await run_github_event(
        t.execute,
        agents.dispatch,
        {"github_event": "pull_request", "github_repo": "o/r", "pr_number": 2},
    )
    key, event_name, data, event_id = t.args("write_history")
    assert (key, event_name, event_id) == ("BGV-105", "pr_state", "2-merged")
    assert data == {
        "repo": "o/r",
        "pr": 2,
        "url": "https://github.com/o/r/pull/2",
        "state": "merged",
        "branch": "feature/BGV-105-limit",
        "files": ["bank/src/TransferService.java"],
    }
    assert all(a != "JiraRequirementReview" for a, _, _ in agents.calls)


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


def test_the_reply_numbers_the_open_questions_so_they_can_be_answered() -> None:
    sections = dict(handoff.memory_sections({"services": [], "questions": QUESTIONS}))
    assert sections["Answered so far"] == ["Q1 Is the limit per account? → Yes, per account."]
    assert sections['Still open — answer in a comment starting "Q<number>:"'] == [
        "Q2 What is the reset time?"
    ]
    assert handoff.memory_sections(None) == []


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


async def test_a_review_with_findings_but_no_questions_keeps_each_finding() -> None:
    # BTS-39, live 2026-10-30: four gaps, none typed "Open Question", and
    # nothing was kept to answer.
    t = _review_tools()
    findings = [
        {"finding_type": "Ambiguity", "description": "Which column header holds the note?"},
        {"finding_type": "Missing Detail", "description": "Is an empty note a blank cell?"},
    ]

    async def dispatch(agent, payload, **options):
        return [reviewed_contract(findings=findings, questions=[])]

    await run_router(PAYLOAD, t.execute, dispatch)
    assert t.args("keep_card")[2] == [
        "Which column header holds the note?",
        "Is an empty note a blank cell?",
    ]


async def test_a_review_shown_again_still_keeps_and_shows_the_memory() -> None:
    # BTS-39, live 2026-10-30: the unchanged ticket's review was shown again
    # and the memory was skipped.
    from tests.test_orchestration import gate as state_gate
    from tests.test_orchestration import state

    t = Tools(
        ingress_check=state_gate(
            "@Aetherion review",
            state(review_score=4),
            commands={**ON, "repos": "bank: AetherionAgentDemo/bank-transfer-service"},
        ),
        story_bundle=BUNDLE,
        recall_card=RECALLED,
        keep_card=KEPT,
    )

    async def dispatch(agent, payload, **options):
        raise AssertionError("a review shown again runs no agent")

    await run_router(PAYLOAD, t.execute, dispatch)
    assert t.count("recall_card") == 1 and t.count("keep_card") == 1
    reply = t.reply_text()
    assert "shown again" in reply and "Services this card touches (1 = the main one)" in reply
    assert "Q2 What is the reset time?" in reply


async def test_a_story_not_ready_lists_its_questions_numbered() -> None:
    # BTS-38, live 2026-10-30: the questions were kept as Q1-Q4 but shown without numbers.
    t, agents = handoff_tools(), Agents()

    async def low(agent, payload, **options):
        if agent == "JiraRequirementReview":
            return [reviewed(2, ["Is the limit per account?", "What is the reset time?"])]
        return await agents.dispatch(agent, payload, **options)

    agents_low = Agents()
    agents_low.dispatch = low  # type: ignore[method-assign]
    out = await run_command(
        command(
            "TDD", "@Aetherion create TDD", t, agents_low, settings={**ON, "tdd_min_readiness": 4}
        )
    )
    sections = dict(out.blocks)
    assert sections['Open questions — answer in a comment starting "Q<number>:"'] == [
        "Q2 What is the reset time?"
    ]


def test_a_file_that_could_not_be_read_is_named_not_sent_empty():
    """Planning refuses a file with no text at all (2026-11-03)."""
    bundle = {"root": {"key": "BTS-40", "type": "Bug", "summary": "s", "description": "d"}}
    card = handoff.card(
        bundle,
        [
            {"filename": "error.png", "text": "a shifted toolbar"},
            {"filename": "call.mp3", "text": ""},
        ],
    )
    assert card["files"] == [
        {"name": "error.png", "content": "a shifted toolbar"},
        {"name": "call.mp3", "description": "attached to the card; its content could not be read"},
    ]


def test_planning_reads_the_comments_and_notes_in_the_description():
    """Planning reads the card's title, description and files (2026-11-03)."""
    card = {"key": "BTS-40", "type": "Bug", "title": "t", "description": "Shift on select.",
            "comments": ["Chrome 130", "  "], "files": []}
    sent = handoff.payload(card, [{"service": "ui"}], [], notes="Agreed now: keep it disabled.")
    assert sent["card"]["description"] == (
        "Shift on select.\n\nComments on the card:\n- Chrome 130"
        "\n\nFor this version:\nAgreed now: keep it disabled."
    )
    assert sent["card"]["comments"] == ["Chrome 130", "  "]  # left as they were
