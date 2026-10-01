"""Duplicate detection sees every ticket, fast (2026-09-30).

It read only the newest 300 (LTW_MAX_CONTEXT_ISSUES): in an 18,000-ticket
project 17,700 were never compared. The index holds the whole project in
memory, asks Jira only what changed, and compares a title only with tickets
that share a word with it — which must find exactly what comparing with every
ticket finds.
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx
import pytest

from src.jira import api as jira
from src.jira import ticket_index
from src.models.schemas import ExistingIssue
from src.tools import tools

WORDS = (
    "candidate verification report invoice client billing employer address police education "
    "consent reminder email portal recruiter officer upload document status check result export "
    "dashboard notify schedule payment contract approval workflow review audit record history"
).split()


def project(n: int, seed: int = 7) -> list[ExistingIssue]:
    rnd = random.Random(seed)

    def text(k: int) -> str:
        return " ".join(rnd.choice(WORDS) for _ in range(k))

    return [
        ExistingIssue(
            key=f"BGV-{i}",
            summary=text(5).capitalize(),
            issue_type="Story",
            status="To Do",
            description=". ".join(text(12) for _ in range(6)),
        )
        for i in range(1, n + 1)
    ]


def indexed(issues: list[ExistingIssue]) -> ticket_index.ProjectIndex:
    index = ticket_index.ProjectIndex("BGV")
    for issue in issues:
        index.put(issue)
    return index


def results(titles: list[str], requirement: str, existing: list[ExistingIssue]) -> tuple:
    report = jira.build_overlap_report(titles, existing, requirement=requirement)
    near = jira.find_near_misses(titles, existing)
    key = lambda m: (m.proposed_title, m.existing_key, m.score, m.matched_on)  # noqa: E731
    return sorted(map(key, report.matches)), sorted(map(key, near))


@pytest.mark.parametrize("seed", range(12))
def test_the_index_finds_exactly_what_comparing_every_ticket_finds(seed) -> None:
    issues = project(2000)
    rnd = random.Random(seed)
    titles = [" ".join(rnd.choice(WORDS) for _ in range(5)).capitalize() for _ in range(6)]
    titles.append(issues[rnd.randrange(len(issues))].summary)  # a certain duplicate
    requirement = ". ".join(" ".join(rnd.choice(WORDS) for _ in range(10)) for _ in range(5))

    # Jira returns the project newest first (ORDER BY created DESC), and the
    # rules keep the first of equal scores — so the comparison is in that order.
    everything = results(titles, requirement, list(reversed(issues)))
    candidates = indexed(issues).candidates(titles, requirement)
    assert results(titles, requirement, candidates) == everything
    assert everything[0], "the planted duplicate is found"


def test_a_ticket_sharing_no_word_is_never_a_candidate() -> None:
    index = indexed(
        [
            ExistingIssue(key="BGV-1", summary="Send invoice reminder", issue_type="Story"),
            ExistingIssue(key="BGV-2", summary="Upload police clearance", issue_type="Story"),
        ]
    )
    assert [i.key for i in index.candidates(["Email the invoice"], "")] == ["BGV-1"]


def test_an_edited_ticket_replaces_its_old_words() -> None:
    index = indexed([ExistingIssue(key="BGV-1", summary="Send invoice", issue_type="Story")])
    index.put(ExistingIssue(key="BGV-1", summary="Upload police clearance", issue_type="Story"))
    assert index.candidates(["invoice"], "") == []
    assert [i.key for i in index.candidates(["police"], "")] == ["BGV-1"]


def test_it_is_fast_at_18000_tickets() -> None:
    """Real tickets use thousands of different words, not the 33 above."""
    rnd = random.Random(3)
    vocabulary = [f"{rnd.choice(WORDS)}{n}" for n in range(3000)]

    def text(k: int) -> str:
        return " ".join(rnd.choice(vocabulary) for _ in range(k))

    issues = [
        ExistingIssue(
            key=f"BGV-{i}",
            summary=text(5),
            issue_type="Story",
            description=". ".join(text(12) for _ in range(6)),
        )
        for i in range(18000, 0, -1)
    ]
    index = indexed(issues)
    titles = [issues[5].summary, text(4), text(5)]
    requirement = text(40)

    full_result = results(titles, requirement, issues)  # warms the word cache for both

    started = time.perf_counter()
    candidates = index.candidates(titles, requirement)
    indexed_result = results(titles, requirement, candidates)
    indexed_seconds = time.perf_counter() - started

    started = time.perf_counter()
    results(titles, requirement, issues)
    everything_seconds = time.perf_counter() - started
    assert indexed_result == full_result
    # Measured 2026-09-30: 0.03 s vs 0.08 s once words are cached.
    assert indexed_seconds < everything_seconds, (indexed_seconds, everything_seconds)


# --- keeping it up to date ---------------------------------------------------------


class Jira:
    """Answers the two searches the index makes, and records them."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        self.queries: list[str] = []

    async def get(self, url: str, params: dict | None = None, **_: Any):
        from tests.fakes import FakeResponse

        jql = (params or {}).get("jql", "")
        self.queries.append(jql)
        wanted = [i for i in self.issues if "updated >=" not in jql or i.get("changed")]
        return FakeResponse(200, {"issues": wanted, "isLast": True})


def raw(key: str, summary: str, changed: bool = False) -> dict[str, Any]:
    return {
        "key": key,
        "fields": {"summary": summary, "issuetype": {"name": "Story"}, "status": {"name": "To Do"}},
        "changed": changed,
    }


async def test_the_first_build_reads_everything_and_later_ones_only_what_changed() -> None:
    fake = Jira([raw("BGV-1", "Send invoice"), raw("BGV-2", "Upload report")])
    index = await ticket_index.up_to_date(fake, "BGV", "")
    assert len(index.issues) == 2 and "updated >=" not in fake.queries[0]

    fake.issues = [raw("BGV-1", "Send invoice reminder", changed=True), raw("BGV-2", "x")]
    index = await ticket_index.up_to_date(fake, "BGV", "")
    assert 'updated >= "-' in fake.queries[1]
    assert index.issues["BGV-1"].summary == "Send invoice reminder"
    assert index.issues["BGV-2"].summary == "Upload report", "unchanged tickets are not re-read"


async def test_a_full_re_read_drops_deleted_tickets(monkeypatch) -> None:
    now = [1_000_000.0]
    monkeypatch.setattr(ticket_index.time, "time", lambda: now[0])
    monkeypatch.setenv("LTW_INDEX_FULL_REFRESH_HOURS", "6")
    fake = Jira([raw("BGV-1", "Send invoice"), raw("BGV-2", "Upload report")])
    await ticket_index.up_to_date(fake, "BGV", "")

    fake.issues = [raw("BGV-1", "Send invoice")]  # BGV-2 deleted
    now[0] += 5 * 3600
    assert "BGV-2" in (await ticket_index.up_to_date(fake, "BGV", "")).issues, "not yet"
    now[0] += 2 * 3600
    assert set((await ticket_index.up_to_date(fake, "BGV", "")).issues) == {"BGV-1"}


async def test_jira_unreadable_falls_back_to_the_newest_tickets(monkeypatch, jira_env) -> None:
    async def down(*_a, **_k):
        raise httpx.ConnectError("jira down")

    monkeypatch.setattr(ticket_index, "up_to_date", down)
    newest = [ExistingIssue(key="BGV-9", summary="Send invoice", issue_type="Story")]
    assert await tools._every_ticket("BGV", ["Send invoice"], "", newest) == newest


async def test_the_index_can_be_switched_off(monkeypatch, jira_env) -> None:
    monkeypatch.setenv("LTW_DUPLICATE_INDEX", "false")
    newest = [ExistingIssue(key="BGV-9", summary="Send invoice", issue_type="Story")]
    assert await tools._every_ticket("BGV", ["Send invoice"], "", newest) == newest
