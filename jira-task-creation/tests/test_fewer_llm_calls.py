"""Fewer model calls, the same answers (2026-09-29).

A normal build made three calls at least — triage, breakdown, self-review —
and up to nine. Each call below was removed only where its answer was already
known; each test counts the calls, so a removed call cannot quietly return.

L1  triage is not asked about a request that lists its requirements
L2  both kinds of duplicate question go in one call, not two
L3  sharpening criteria and carrying missing facts go in one call, not two
L4  a ticket that shares no words with the request is unrelated without asking
"""

from __future__ import annotations

import json

import pytest

from src.classification import decompose
from src.models.schemas import DuplicateMatch, ExistingIssue, WorkBreakdown
from src.tools import tools
from tests.test_bare_build_comment import BGV3_DESCRIPTION
from tests.test_details import COVERAGE, LINES, _two_stories


@pytest.fixture
def calls(monkeypatch) -> list[str]:
    """Every prompt sent to the model; answers an empty JSON object."""
    sent: list[str] = []

    async def chat(prompt: str) -> str:
        sent.append(prompt)
        return json.dumps({"verdict": "VALID", "reason": "ok", "questions": []})

    monkeypatch.setattr(decompose, "_chat", chat)
    return sent


# --- L1 -------------------------------------------------------------------------------


async def test_a_listed_requirement_is_not_triaged_by_the_model(calls) -> None:
    result = await tools.validate_requirement(BGV3_DESCRIPTION, {})
    assert result["status"] == "READY_FOR_JIRA"
    assert result["generator"] == "rules"
    assert calls == []


async def test_a_one_line_request_is_still_triaged(calls) -> None:
    await tools.validate_requirement("Let recruiters send reminder emails to referees.", {})
    assert len(calls) == 1


# --- L2 -------------------------------------------------------------------------------


def match(title: str, key: str, on: str) -> DuplicateMatch:
    return DuplicateMatch(
        proposed_title=title, existing_key=key, existing_summary=title, score=0.6, matched_on=on
    )


async def test_description_matches_and_near_misses_share_one_question(monkeypatch) -> None:
    asked: list[list] = []

    async def same_work(pairs):
        asked.append(pairs)
        return {0: True, 1: True}  # the description match, and the near miss

    near = match("Log a rest break", "FL-68", "summary")
    monkeypatch.setattr(tools, "llm_same_work", same_work)
    monkeypatch.setattr(tools.jira, "find_near_misses", lambda *a, **k: [near])

    kept = await tools._second_opinion(
        [match("Send invoices", "BGV-30", "description")],
        ["Send invoices", "Log a rest break"],
        [ExistingIssue(key="FL-68", summary="Allow drivers to log a rest break")],
        "https://x.atlassian.net",
        set(),
    )

    assert len(asked) == 1 and len(asked[0]) == 2
    assert [(m.proposed_title, m.matched_on) for m in kept] == [
        ("Send invoices", "description"),
        ("Log a rest break", "semantic"),
    ]


async def test_without_the_model_each_kind_falls_back_as_before(monkeypatch) -> None:
    async def down(pairs):
        raise ConnectionError("no gateway")

    monkeypatch.setattr(tools, "llm_same_work", down)
    monkeypatch.setattr(
        tools.jira, "find_near_misses", lambda *a, **k: [match("Log a break", "FL-68", "summary")]
    )
    kept = await tools._second_opinion(
        [match("Send invoices", "BGV-30", "description")],
        ["Send invoices", "Log a break"],
        [ExistingIssue(key="FL-68", summary="Log a break")],
        "https://x.atlassian.net",
        set(),
    )
    # A description match stands; a near miss is no match.
    assert [m.proposed_title for m in kept] == ["Send invoices"]


# --- L3 -------------------------------------------------------------------------------


def _vague_and_missing() -> WorkBreakdown:
    breakdown = _two_stories(["Given an invoice, when sent, then it reaches the contact"])
    data = breakdown.model_dump(mode="json")
    data["stories"][0]["subtasks"][0][
        "completion_criteria"
    ] = "Email PDF is implemented and tested."
    return WorkBreakdown.model_validate(data)


async def test_sharpening_and_carrying_share_one_call(monkeypatch) -> None:
    sent: list[str] = []
    good = "Given an invoice dated 1 June, when it is sent, then its due date is 30 days later"
    concrete = "A June invoice for a client with 3 checks reaches every billing contact on 1 July."

    async def chat(prompt: str) -> str:
        sent.append(prompt)
        return json.dumps(
            {
                "criteria": {"Send Invoices via Email": [good]},
                "completion": {"Email PDF": concrete},
            }
        )

    monkeypatch.setattr(decompose, "_chat", chat)
    repaired = await decompose._repair(
        _vague_and_missing(), "- " + "\n- ".join(LINES), LINES, COVERAGE
    )

    assert len(sent) == 1
    assert "Some stated facts" in sent[0] and "Rewrite ONLY the completion criteria" in sent[0]
    send = repaired.stories[0]
    assert send.acceptance_criteria[-1] == good
    assert send.subtasks[0].completion_criteria == concrete


async def test_a_failed_repair_call_still_carries_every_fact(monkeypatch) -> None:
    async def down(prompt: str) -> str:
        raise ConnectionError("no gateway")

    monkeypatch.setattr(decompose, "_chat", down)
    repaired = await decompose._repair(_vague_and_missing(), "req", LINES, COVERAGE)
    assert repaired.stories[0].acceptance_criteria[-1] == (
        'As stated in the requirement: "Due date is 30 days after the invoice date."'
    )


# --- L4 -------------------------------------------------------------------------------


class _Ticket:
    """Just enough of a client for read_root."""

    def __init__(self, summary: str, text: str = "") -> None:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]
        self.body = {
            "key": "BGV-1",
            "fields": {
                "summary": summary,
                "description": {"type": "doc", "version": 1, "content": content if text else []},
                "issuetype": {"name": "Story"},
            },
        }

    async def get(self, url: str, **kwargs):
        from tests.fakes import FakeResponse

        return FakeResponse(200, self.body)


@pytest.mark.parametrize(
    "summary,text,expected",
    [
        ("Agent test ticket", "Used to check the agent responds.", False),
        ("Verification exports", "", True),
    ],
)
async def test_clear_words_decide_without_the_model(calls, summary, text, expected) -> None:
    related = await tools._is_related(
        _Ticket(summary, text), "BGV-1", "Export verification results to Excel."
    )
    assert related is expected
    assert calls == []


async def test_unclear_words_ask_the_model(monkeypatch) -> None:
    asked: list[str] = []

    async def chat(prompt: str) -> str:
        asked.append(prompt)
        return json.dumps({"related": True, "reason": "same audit reports"})

    monkeypatch.setattr(decompose, "_chat", chat)
    related = await tools._is_related(
        _Ticket("Audit results page", ""), "BGV-1", "Export verification results to Excel."
    )
    assert related is True and len(asked) == 1
