"""The TDD, design and code commands (2026-10-08).

on the story   @Aetherion create TDD      readiness review → Planning → Confluence → link
on the page    @Aetherion explain why …   Planning answers from the TDD and the code
               @Aetherion update the TDD  Planning revises; the page gets a new version
               @Aetherion code            branches proposed → confirmed → PR on page + story
in GitHub      a merge                    the graph is refreshed; story and page are told
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.command_flow import Command, run_command
from agent.confluence_flow import run_confluence_comment
from agent.router_flow import run_github_event, run_router
from routing.classify import classify_by_verb
from routing.commands import BranchPlan, blocks_to_storage, branch_slug, parse_code_command
from routing.compose import HELP_OPTIONS
from routing.orchestration import TicketState, previous_review_points
from shared.adf import blocks_to_doc
from shared.contract import AgentResult, Outcome
from tests.fake_tools import Tools
from tests.test_orchestration import Clock, gate

PAGE = {
    "ok": True,
    "page_id": "9001",
    "title": "BGV-105 - TDD v1.0",
    "url": "https://site/wiki/spaces/PR/pages/9001",
    "version": 1,
    "text": "1. Overview — a daily limit in TransferService",
    "issue_key": "BGV-105",
    "code_plan": {},
    "discussion": ["Priya: why a Clock?"],
}
STORY = {
    "ok": True,
    "key": "BGV-105",
    "summary": "Daily transfer limit",
    "type": "Story",
    "is_subtask": False,
    "parent_key": "",
    "project_key": "BGV",
    "project_name": "Praman",
    "subtask_keys": ["BGV-106", "BGV-107"],
    "tdd": {},
}
SETTINGS = {
    "repos": "bank: AetherionAgentDemo/bank-transfer-service",
    "planning_agent": "planning_agent",
    "planning_queue": "",
    "planning_timeout_minutes": 15,
    "coding_agent": "coding-agent",
    "coding_queue": "",
    "coding_timeout_minutes": 20,
    "coding_attempts": 2,
    "tdd_min_readiness": 4,
    "task_queue_overrides": {},
}
PUBLISHED = {
    "ok": True,
    "created": True,
    "space_created": False,
    "page_id": "9001",
    "url": PAGE["url"],
    "title": PAGE["title"],
    "space_key": "PR",
    "space_name": "Praman",
    "version": 1,
}
DOCUMENT = {
    "title": "Daily limit",
    "confluence_html": "<h1>Daily limit</h1>",
    "text": "1. Overview",
}
PR = "https://github.com/AetherionAgentDemo/bank-transfer-service/pull/2"


def reviewed(score: int, questions: list[str] | None = None) -> dict:
    return AgentResult(
        produced_by="JiraRequirementReview",
        display_name="Requirement Review",
        outcome=Outcome.REVIEWED,
        headline="Requirement review",
        summary=f"Readiness {score}/5.",
        readiness_score=score,
        questions=questions if questions is not None else ["Is the limit per account?"],
    ).to_dict()


def coded(pr: str = PR) -> dict:
    return {
        "status": "success",
        "pull_requests": [
            {
                "service": "bank",
                "pr_url": pr,
                "tests_ran": True,
                "tests_passed": True,
                "branch": "feature/BGV-105-daily-transfer-limit",
                "base": "main",
            }
        ],
        "skipped": [],
    }


class Agents:
    def __init__(self, **answers: list[Any]) -> None:
        self.answers = {
            "JiraRequirementReview": [reviewed(4)],
            "planning_agent": [{"status": "success", "results": [], "document": DOCUMENT}],
            "coding-agent": [coded()],
            **answers,
        }
        self.calls: list[tuple[str, dict]] = []

    async def dispatch(self, agent: str, payload: dict, **options: Any) -> Any:
        self.calls.append((agent, payload))
        queue = self.answers[agent]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return [answer] if agent == "JiraRequirementReview" else answer

    def names(self) -> list[str]:
        return [a for a, _ in self.calls]

    def payload(self, agent: str, n: int = 0) -> dict:
        return [p for a, p in self.calls if a == agent][n]


def tools(**over: Any) -> Tools:
    return Tools(
        story_context=over.pop("story_context", STORY),
        planning_input={"issue_text": "Requirement BGV-105: Daily transfer limit"},
        publish_tdd=over.pop("publish_tdd", PUBLISHED),
        read_tdd=over.pop("read_tdd", PAGE),
        branch_defaults={
            "repo": "AetherionAgentDemo/bank-transfer-service",
            "source": "main",
            "branch": "feature/BGV-105-daily-transfer-limit",
            "target": "main",
        },
        save_code_plan={"saved": True},
        **over,
    )


def command(intent: str, text: str, t: Tools, agents: Agents, **over: Any) -> Command:
    said: list = []

    async def say(blocks):
        said.append(blocks)

    return Command(
        intent=intent,
        text=text,
        issue_key=over.pop("issue_key", "BGV-105"),
        run_id="r1",
        execute=t.execute,
        dispatch=agents.dispatch,
        say=say,
        settings=over.pop("settings", SETTINGS),
        keyword="Aetherion",
        review_spec=_review_spec(),
        **over,
    )


def _review_spec():
    from routing.catalog import by_intent

    return by_intent("REVIEW")


def text(blocks) -> str:
    return str(blocks)


# --- help and routing ----------------------------------------------------------------


def test_help_lists_the_eight_commands() -> None:
    """2026-10-15: "status" joined the seven."""
    joined = " ".join(HELP_OPTIONS)
    for cmd in (
        "build",
        "review",
        "explain this card",
        "explain why",
        "create TDD",
        "code",
        "status",
        "help",
    ):
        assert f"@Aetherion {cmd}" in joined, cmd
    assert len(HELP_OPTIONS) == 8


@pytest.mark.parametrize(
    "comment,intent",
    [
        ("@Aetherion create TDD", "TDD"),
        ("@Aetherion explain why this approach was selected", "DESIGN"),
        ("@Aetherion why can't we use a limits table?", "DESIGN"),
        ("@Aetherion update the TDD", "UPDATE_TDD"),
        ("@Aetherion code", "CODE"),
        ("@Aetherion explain this card", "QUESTION"),
        ("@Aetherion review", "REVIEW"),
        ("@Aetherion build", "BUILD"),
    ],
)
def test_each_command_routes_by_its_words(comment, intent) -> None:
    assert classify_by_verb(comment).intent == intent


def test_links_in_a_reply_can_be_clicked() -> None:
    doc = blocks_to_doc([("Created", ["Sub-task [BGV-106](https://s/browse/BGV-106) — x"])])
    run = doc["content"][1]["content"][0]["content"][0]["content"][1]
    assert (
        run["text"] == "BGV-106" and run["marks"][0]["attrs"]["href"] == "https://s/browse/BGV-106"
    )


# --- create TDD ------------------------------------------------------------------------


async def test_a_ready_story_gets_its_tdd_in_confluence_and_the_link() -> None:
    t, agents = tools(), Agents()
    result = await run_command(command("TDD", "@Aetherion create TDD", t, agents))

    assert agents.names() == ["JiraRequirementReview", "planning_agent"]
    assert agents.payload("planning_agent")["return_document"] is True
    args = t.args("publish_tdd")
    assert args[:4] == ("BGV-105", "BGV", "Praman", "Daily transfer limit")
    assert args[5] == "<h1>Daily limit</h1>"
    assert PAGE["url"] in text(result.blocks) and result.links["tdd"] == PAGE["url"]
    assert "Readiness 4/5" in text(result.blocks)
    assert result.review["readiness_score"] == 4


async def test_open_questions_stop_the_tdd_and_are_asked_on_the_story() -> None:
    t = tools()
    agents = Agents(JiraRequirementReview=[reviewed(3, ["What is the limit's currency?"])])
    result = await run_command(command("TDD", "@Aetherion create TDD", t, agents))

    assert "planning_agent" not in agents.names() and t.count("publish_tdd") == 0
    assert "What is the limit's currency?" in text(result.blocks)
    assert "3/5" in text(result.blocks) and "then ask again" in text(result.blocks)


async def test_a_reviewed_unchanged_story_is_not_reviewed_again() -> None:
    t, agents = tools(), Agents()
    await run_command(
        command("TDD", "@Aetherion create TDD", t, agents, reusable_review=reviewed(5))
    )
    assert agents.names() == ["planning_agent"]


async def test_the_tdd_is_asked_for_on_the_story_not_a_sub_task() -> None:
    t = tools(story_context={**STORY, "is_subtask": True, "parent_key": "BGV-105"})
    agents = Agents()
    result = await run_command(
        command("TDD", "@Aetherion create TDD", t, agents, issue_key="BGV-106")
    )
    assert agents.calls == []
    assert "sub-task" in text(result.blocks) and "BGV-105" in text(result.blocks)


async def test_the_review_reads_the_answers_to_its_previous_questions() -> None:
    t, agents = tools(), Agents()
    await run_command(
        command("TDD", "@Aetherion create TDD", t, agents, previous_questions=["Per account?"])
    )
    assert agents.payload("JiraRequirementReview")["previous_questions"] == ["Per account?"]


# --- design questions and updates -----------------------------------------------------


async def test_a_design_question_goes_to_planning_with_the_tdd() -> None:
    t = tools()
    agents = Agents(
        planning_agent=[
            {
                "status": "success",
                "answer": "The history already has every transfer.",
                "change_recommended": True,
                "proposed_change": "Keep a running total per day.",
            }
        ]
    )
    result = await run_command(
        command("DESIGN", "@Aetherion why not a separate limits table?", t, agents, page_id="9001")
    )
    sent = agents.payload("planning_agent")
    assert sent["mode"] == "discuss" and sent["question"] == "why not a separate limits table?"
    assert sent["tdd_text"] == PAGE["text"] and sent["discussion"] == PAGE["discussion"]
    assert "Keep a running total per day." in text(result.blocks)
    assert '"@Aetherion update the TDD"' in text(result.blocks)


async def test_without_a_tdd_the_story_is_told_to_create_one() -> None:
    t = tools()
    result = await run_command(command("DESIGN", "@Aetherion why X?", t, Agents()))
    assert "create TDD" in text(result.blocks)


async def test_update_revises_the_page_with_the_discussion() -> None:
    t, agents = tools(), Agents()
    result = await run_command(
        command(
            "UPDATE_TDD",
            "@Aetherion update the TDD: use a running total",
            t,
            agents,
            page_id="9001",
        )
    )
    issue_text = agents.payload("planning_agent")["issue_text"]
    assert "The current technical design" in issue_text and "Priya: why a Clock?" in issue_text
    assert "Asked now: update the TDD: use a running total" in issue_text
    assert t.count("publish_tdd") == 1 and "TDD updated" in text(result.blocks)


# --- code ------------------------------------------------------------------------------


async def test_code_first_asks_to_confirm_the_branches() -> None:
    t, agents = tools(), Agents()
    result = await run_command(command("CODE", "@Aetherion code", t, agents, page_id="9001"))

    assert agents.calls == [], "nothing is coded before the branches are confirmed"
    reply = text(result.blocks)
    assert "Start from (source): main" in reply
    assert "New working branch: feature/BGV-105-daily-transfer-limit" in reply
    assert '"@Aetherion code confirm"' in reply
    assert (
        t.args("save_code_plan")[1]["proposed"]["branch"] == "feature/BGV-105-daily-transfer-limit"
    )


async def test_confirmed_branches_go_to_coding_with_the_tdd_and_the_requirements() -> None:
    plan = {"proposed": {"source": "main", "branch": "feature/BGV-105-x", "target": "main"}}
    t, agents = tools(read_tdd={**PAGE, "code_plan": plan}), Agents()
    result = await run_command(
        command("CODE", "@Aetherion code confirm", t, agents, page_id="9001")
    )

    sent = agents.payload("coding-agent")
    assert sent["branches"] == {"source": "main", "branch": "feature/BGV-105-x", "target": "main"}
    assert sent["tdd_text"] == PAGE["text"] and "BGV-105" in sent["requirements"]
    assert (
        sent["repos"] == "bank: AetherionAgentDemo/bank-transfer-service"
    ), "Coding writes only to the configured repositories"
    assert PR in text(result.blocks) and result.links == {"pr": PR, "page_id": "9001"}


async def test_branches_can_be_named_in_the_command() -> None:
    t, agents = tools(), Agents()
    await run_command(
        command(
            "CODE",
            "@Aetherion code source=develop branch=feature/limit target=release",
            t,
            agents,
            page_id="9001",
        )
    )
    assert agents.payload("coding-agent")["branches"] == {
        "source": "develop",
        "branch": "feature/limit",
        "target": "release",
    }


async def test_a_design_coding_cannot_implement_is_revised_and_tried_again() -> None:
    no_pr = {
        "status": "no_pull_requests",
        "pull_requests": [],
        "skipped": [{"service": "bank", "file_path": "X.java", "reason": "not in the clone"}],
    }
    t = tools(
        read_tdd={
            **PAGE,
            "code_plan": {"proposed": {"source": "main", "branch": "f", "target": "main"}},
        }
    )
    agents = Agents(**{"coding-agent": [no_pr, coded()]})
    result = await run_command(
        command("CODE", "@Aetherion code confirm", t, agents, page_id="9001")
    )

    assert agents.names() == ["coding-agent", "planning_agent", "coding-agent"]
    assert "Coding could not implement the design" in agents.payload("planning_agent")["issue_text"]
    assert PR in text(result.blocks)


def test_branch_commands_are_read() -> None:
    assert parse_code_command("@Aetherion code confirm") == (BranchPlan(), True)
    plan, confirmed = parse_code_command("@Aetherion code source=main branch=feature/x target=dev")
    assert plan == BranchPlan("main", "feature/x", "dev") and not confirmed
    assert branch_slug("bgv-105", "Daily transfer limit!") == "feature/BGV-105-daily-transfer-limit"


def test_a_confluence_reply_keeps_links_and_escapes_text() -> None:
    html = blocks_to_storage([("Pull request", ["bank: https://x/pull/2 (tests passed)", "<b>"])])
    assert '<a href="https://x/pull/2">https://x/pull/2</a>' in html and "&lt;b&gt;" in html


# --- through the router (a Jira comment) ----------------------------------------------


async def test_create_tdd_on_jira_answers_in_one_edited_reply() -> None:
    t = tools(ingress_check=gate("@Aetherion create TDD", commands=SETTINGS))
    agents = Agents()
    out = await run_router(
        {"issue_key": "BGV-105", "comment_id": "1"}, t.execute, agents.dispatch, Clock().sleep
    )

    assert out["intent"] == "TDD" and t.count("post_reply") == 1
    assert PAGE["url"] in t.reply_text()
    stored = [a for n, a in t.calls if n == "finish_ticket"]
    assert stored[0][1] == "REVIEW" and stored[0][3]["readiness_score"] == 4, "the review is kept"


async def test_a_pr_from_jira_is_also_linked_on_the_page() -> None:
    plan = {"proposed": {"source": "main", "branch": "f", "target": "main"}}
    t = tools(
        ingress_check=gate("@Aetherion code confirm", commands=SETTINGS),
        story_context={**STORY, "tdd": {"page_id": "9001"}},
        read_tdd={**PAGE, "code_plan": plan},
    )
    await run_router(
        {"issue_key": "BGV-105", "comment_id": "1"}, t.execute, Agents().dispatch, Clock().sleep
    )
    page_id, parent, blocks, _ = t.args("confluence_reply")
    assert page_id == "9001" and PR in str(blocks)


# --- through the page (a Confluence comment) -----------------------------------------


async def test_a_page_comment_is_answered_under_it_and_a_pr_reaches_the_story() -> None:
    event = {"id": "c7", "root_id": "c7", "page_id": "9001", "text": "@Aetherion code confirm"}
    plan = {"proposed": {"source": "main", "branch": "f", "target": "main"}}
    t = tools(
        confluence_comment={
            "ok": True,
            "events": [event],
            "pages": 1,
            "keyword": "Aetherion",
            "settings": SETTINGS,
        },
        confluence_reply={"posted": True, "comment_id": "c8"},
        confluence_update_reply={"updated": True},
        read_tdd={**PAGE, "code_plan": plan},
    )
    out = await run_confluence_comment(t.execute, Agents().dispatch, "9001", "c7")

    names = t.names()
    assert names.index("confluence_mark_answered") < names.index("confluence_reply"), "marked first"
    assert t.args("confluence_reply")[1] == "c7", "threaded under the comment"
    issue_key, blocks, _ = t.args("story_comment")
    assert issue_key == "BGV-105" and PR in str(blocks)
    assert out["answered"][0]["intent"] == "CODE"


async def test_a_page_comment_that_is_no_command_gets_the_page_help() -> None:
    event = {"id": "c9", "page_id": "9001", "text": "@Aetherion hello"}
    t = tools(
        confluence_comment={
            "ok": True,
            "events": [event],
            "pages": 1,
            "keyword": "Aetherion",
            "settings": SETTINGS,
        },
        confluence_reply={"posted": True, "comment_id": "c10"},
    )
    out = await run_confluence_comment(t.execute, Agents().dispatch, "9001", "c9")
    assert out["answered"][0]["intent"] == "HELP"
    assert "update the TDD" in str(t.args("confluence_reply")[2])


# --- a merge -------------------------------------------------------------------------


async def test_a_merge_is_announced_on_the_story_after_the_graph_refresh() -> None:
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
    }
    t = Tools(
        github_event=scan, github_event_done={"recorded": True}, announce_merge={"jira": True}
    )

    async def planning(name, payload, **options):
        return {"status": "success", "mode": "graph_sync"}

    await run_github_event(
        t.execute, planning, {"github_event": "pull_request", "github_repo": "o/r", "pr_number": 2}
    )
    assert t.args("announce_merge") == ("BGV-105", 2, "https://github.com/o/r/pull/2", "o/r")


# --- review: answers in comments count -------------------------------------------------


def test_a_new_answer_in_the_comments_makes_the_ticket_changed() -> None:
    from tools.tools import content_fingerprint

    def comment(cid: str, text: str) -> dict:
        return {
            "id": cid,
            "created": "t",
            "body": {
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
            },
        }

    base = {"summary": "S", "description": None, "attachment": [], "comment": {"comments": []}}
    asked = {**base, "comment": {"comments": [comment("1", "@Aetherion review")]}}
    replied = {
        **base,
        "comment": {"comments": [comment("2", "AetherionAgent · Review — automated reply")]},
    }
    answered = {**base, "comment": {"comments": [comment("3", "The limit is per account.")]}}
    assert content_fingerprint(base) == content_fingerprint(asked) == content_fingerprint(replied)
    assert content_fingerprint(base) != content_fingerprint(answered)


def test_the_previous_reviews_questions_are_carried_forward() -> None:
    state = TicketState.from_dict({"last": {"REVIEW": {"result": reviewed(3, ["Per account?"])}}})
    assert previous_review_points(state) == ["Per account?"]


async def test_a_coding_timeout_is_reported_not_sent_back_to_planning() -> None:
    """BGV-105 (2026-10-08): a timeout was taken for a design problem and revised twice."""
    crashed = {"status": "error", "message": "ActivityError: Activity task timed out"}
    t = tools(
        read_tdd={
            **PAGE,
            "code_plan": {"proposed": {"source": "main", "branch": "f", "target": "main"}},
        }
    )
    agents = Agents(**{"coding-agent": [crashed]})
    result = await run_command(
        command("CODE", "@Aetherion code confirm", t, agents, page_id="9001")
    )
    assert agents.names() == ["coding-agent"], "no revision, no second attempt"
    assert t.count("publish_tdd") == 0
    assert "timed out" in text(result.blocks) and "design was not changed" in text(result.blocks)


def test_the_branch_question_names_the_keyword_in_use() -> None:
    from routing.commands import branch_question

    lines = str(branch_question(BranchPlan("main", "f", "main"), "o/r", "AetherionLocal"))
    assert "@AetherionLocal code confirm" in lines


def failing(pr: str) -> dict:
    return {
        "status": "success",
        "pull_requests": [
            {
                "service": "bank",
                "pr_url": pr,
                "tests_ran": True,
                "tests_passed": False,
                "verification_detail": "[ERROR] COMPILATION ERROR :\n[ERROR] /private/x/repo_fetcher_ab12/"
                "src/main/java/com/bank/TransferService.java:[43,35] constructor Transaction cannot be applied",
                "branch": "f",
                "base": "main",
            }
        ],
        "skipped": [],
    }


async def test_a_pr_whose_tests_fail_goes_back_to_planning_and_is_replaced() -> None:
    """BGV-105 PR #8 (2026-10-08): it compiled against a Transaction it never changed."""
    t = tools(
        read_tdd={
            **PAGE,
            "code_plan": {"proposed": {"source": "main", "branch": "f", "target": "main"}},
        }
    )
    passing = {
        **coded("https://github.com/o/r/pull/9"),
        "closed": ["https://github.com/o/r/pull/8"],
    }
    agents = Agents(**{"coding-agent": [failing("https://github.com/o/r/pull/8"), passing]})
    result = await run_command(
        command("CODE", "@Aetherion code confirm", t, agents, page_id="9001")
    )

    assert agents.names() == ["coding-agent", "planning_agent", "coding-agent"]
    revised = agents.payload("planning_agent")["issue_text"]
    assert (
        "tests failed" in revised
        and "TransferService.java:[43,35] constructor Transaction" in revised
    )
    assert "repo_fetcher_ab12" not in revised, "no laptop temp paths in the design"
    assert agents.payload("coding-agent", 1)["supersedes"] == ["https://github.com/o/r/pull/8"]
    reply = text(result.blocks)
    assert "pull/9" in reply and "pull/8 (closed — its tests failed)" in reply
    assert "Pull request raised" in reply and "FAILED" not in reply.split("Replaced")[0]


async def test_tests_failing_on_the_last_attempt_are_said_plainly() -> None:
    t = tools(
        read_tdd={
            **PAGE,
            "code_plan": {"proposed": {"source": "main", "branch": "f", "target": "main"}},
        }
    )
    agents = Agents(**{"coding-agent": [failing("https://github.com/o/r/pull/8")]})
    result = await run_command(
        command(
            "CODE",
            "@Aetherion code confirm",
            t,
            agents,
            page_id="9001",
            settings={**SETTINGS, "coding_attempts": 1},
        )
    )
    reply = text(result.blocks)
    assert "tests FAILED" in reply and "do not merge it" in reply
    assert "constructor Transaction cannot be applied" in reply


def test_a_tdd_page_is_named_after_its_story_and_version() -> None:
    from routing.commands import tdd_title, tdd_version

    assert tdd_title("bgv-105") == "BGV-105 - TDD v1.0"
    assert [tdd_version(n) for n in (1, 2, 3)] == ["v1.0", "v1.1", "v1.2"]
    assert tdd_title("BGV-105", 3) == "BGV-105 - TDD v1.2"


async def test_the_reply_names_the_tdd_and_its_version() -> None:
    t = tools(publish_tdd={**PUBLISHED, "tdd_version": "v1.0"})
    result = await run_command(command("TDD", "@Aetherion create TDD", t, Agents()))
    assert "BGV-105 - TDD v1.0 ready" in text(result.blocks)


async def test_an_update_says_the_new_version() -> None:
    t = tools(
        publish_tdd={
            **PUBLISHED,
            "title": "BGV-105 - TDD v1.1",
            "tdd_version": "v1.1",
            "version": 2,
        }
    )
    result = await run_command(
        command("UPDATE_TDD", "@Aetherion update the TDD", t, Agents(), page_id="9001")
    )
    assert "TDD updated — v1.1" in text(result.blocks)


async def test_the_health_check_probes_every_agent_on_its_real_queue() -> None:
    """2026-10-13: the probes went to the router's own queue and all timed out."""
    from agent.router_flow import run_health_check

    t = Tools(
        request_settings={
            "task_queue_overrides": {"JiraTaskCreation": "q-tc", "JiraRequirementReview": "q-rv"},
            "pipeline": {**SETTINGS, "planning_queue": "q-plan", "coding_queue": "q-code"},
        },
        smtp_login_check={"ok": True},
    )
    sent: list[tuple[str, str, dict]] = []

    async def dispatch(agent, payload, **options):
        sent.append((agent, options.get("task_queue"), payload))
        return {}

    out = await run_health_check(dispatch, t.execute)
    assert {(a, q) for a, q, _ in sent} == {
        ("JiraTaskCreation", "q-tc"),
        ("JiraRequirementReview", "q-rv"),
        ("planning_agent", "q-plan"),
        ("coding-agent", "q-code"),
    }
    assert all(v["reachable"] for v in out["agents"].values())
    planning = next(p for a, _, p in sent if a == "planning_agent")
    assert planning["repos"] == "" and "issue_text" not in planning, "a probe never designs"


async def test_several_services_are_merged_in_the_designs_order() -> None:
    """2026-10-15: payments must accept the optional amount before orders sends it."""
    plan = {"proposed": {"source": "main", "branch": "f", "target": "main"}}
    two = {
        "pull_requests": [
            {
                "service": "payments",
                "pr_url": "https://github.com/o/payments/pull/12",
                "tests_ran": True,
                "tests_passed": True,
                "verified_by": "ci",
            },
            {
                "service": "orders",
                "pr_url": "https://github.com/o/orders/pull/34",
                "tests_ran": True,
                "tests_passed": True,
                "verified_by": "ci",
            },
        ],
        "rollout_order": ["payments: accepts an optional amount first", "orders: then sends it"],
        "skipped": [],
    }
    t, agents = tools(read_tdd={**PAGE, "code_plan": plan}), Agents(**{"coding-agent": [two]})
    result = await run_command(
        command("CODE", "@Aetherion code confirm", t, agents, page_id="9001")
    )
    reply_text = text(result.blocks)
    assert "Merge and deploy in this order" in reply_text
    assert (
        "1. payments: https://github.com/o/payments/pull/12 — accepts an optional amount first"
        in reply_text
    )
    assert "2. orders: https://github.com/o/orders/pull/34 — then sends it" in reply_text
    assert "(CI passed)" in reply_text


async def test_one_pull_request_has_no_order_section() -> None:
    plan = {"proposed": {"source": "main", "branch": "f", "target": "main"}}
    t, agents = tools(read_tdd={**PAGE, "code_plan": plan}), Agents()
    result = await run_command(
        command("CODE", "@Aetherion code confirm", t, agents, page_id="9001")
    )
    assert "Merge and deploy in this order" not in text(result.blocks)
