"""Reviewing the whole project before creating anything.

Covers the three things that changed so older work is actually considered:
pagination, description-aware matching, and partial-overlap reporting.
"""

from __future__ import annotations

import pytest
from fakes import FakeClient, FakeResponse

from src.jira import api as jira
from src.models.schemas import ExistingIssue


def issue(key: str, summary: str, description: str = "", itype: str = "Story") -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "issuetype": {"name": itype},
            "status": {"name": "To Do"},
            "parent": {},
            "labels": [],
            "description": (
                {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": description}]}
                    ],
                }
                if description
                else None
            ),
        },
    }


def paged(pages: list[list[dict]]):
    """A /search/jql route that hands out one page per call."""
    calls = {"n": 0}

    def handler(_url: str, _kwargs: dict) -> FakeResponse:
        index = calls["n"]
        calls["n"] += 1
        is_last = index >= len(pages) - 1
        return FakeResponse(
            200,
            {
                "issues": pages[index],
                "isLast": is_last,
                "nextPageToken": None if is_last else f"token-{index}",
            },
        )

    return handler


# --- pagination -------------------------------------------------------------


async def test_every_page_of_existing_work_is_read() -> None:
    """Older tickets used to be invisible: only the first 100 were ever read."""
    pages = [
        [issue(f"ORD-{n}", f"Story {n}") for n in range(100)],
        [issue(f"ORD-{n}", f"Story {n}") for n in range(100, 150)],
    ]
    client = FakeClient({"/search/jql": paged(pages)})

    issues, unavailable = await jira.fetch_existing_issues(client, "ORD")

    assert len(issues) == 150, "the second page must be read too"
    assert unavailable == []


async def test_paging_stops_at_the_configured_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTW_MAX_CONTEXT_ISSUES", "10")
    pages = [[issue(f"ORD-{n}", f"Story {n}") for n in range(10)]]
    client = FakeClient({"/search/jql": paged(pages)})

    issues, _ = await jira.fetch_existing_issues(client, "ORD")
    assert len(issues) == 10


async def test_a_failed_page_is_reported_not_swallowed() -> None:
    """A partial context must be declared, never silently assumed complete."""
    calls = {"n": 0}

    def handler(_url: str, _kwargs: dict) -> FakeResponse:
        if calls["n"] == 0:
            calls["n"] += 1
            return FakeResponse(
                200,
                {
                    "issues": [issue("ORD-1", "Story 1")],
                    "isLast": False,
                    "nextPageToken": "t1",
                },
            )
        return FakeResponse(500, {"errorMessages": ["boom"]})

    client = FakeClient({"/search/jql": handler})
    issues, unavailable = await jira.fetch_existing_issues(client, "ORD")

    assert len(issues) == 1
    assert unavailable, "the missing pages must be reported"
    assert "Duplicate detection saw only the 1 newest" in unavailable[0]


async def test_descriptions_and_urls_are_carried() -> None:
    pages = [[issue("ORD-1", "Account access", "Users sign in with an email and password.")]]
    client = FakeClient({"/search/jql": paged(pages)})

    issues, _ = await jira.fetch_existing_issues(client, "ORD", base_url="https://x.atlassian.net")
    assert "sign in with an email" in issues[0].description
    assert issues[0].url == "https://x.atlassian.net/browse/ORD-1"


# --- matching when the wording differs --------------------------------------


def test_work_is_recognised_even_when_the_title_is_worded_differently() -> None:
    """The whole point: a differently-titled ticket still counts as covering it."""
    existing = [
        ExistingIssue(
            key="ORD-9",
            summary="Account access",
            issue_type="Story",
            status="In Progress",
            description="Users sign in with an email address and a password, "
            "and can reset a forgotten password.",
        )
    ]
    matches = jira.find_overlaps(["Create password reset"], existing)

    assert len(matches) == 1
    assert matches[0].existing_key == "ORD-9"
    assert matches[0].matched_on == "description"
    assert "matched on its description" in matches[0].explain()


def test_an_unrelated_description_does_not_match() -> None:
    existing = [
        ExistingIssue(
            key="ORD-9",
            summary="Fuel reporting",
            description="Managers see the total fuel cost per vehicle each month.",
        )
    ]
    assert jira.find_overlaps(["Create password reset"], existing) == []


def test_containment_scores_what_it_says() -> None:
    assert jira.containment("password reset", "users can reset their password") == 1.0
    assert jira.containment("password reset", "users can log out") == 0.0
    assert jira.containment("", "anything") == 0.0


# --- full vs partial --------------------------------------------------------


def existing_login_work() -> list[ExistingIssue]:
    return [
        ExistingIssue(key="ORD-4", summary="Create logout", issue_type="Story", status="Done"),
        ExistingIssue(
            key="ORD-9",
            summary="Account access",
            issue_type="Story",
            status="In Progress",
            description="Users sign in with an email address and a password, "
            "and can reset a forgotten password.",
        ),
    ]


def test_everything_already_existing_is_reported_as_full() -> None:
    report = jira.build_overlap_report(
        ["Create logout", "Create password reset"], existing_login_work()
    )
    assert report.is_full
    assert not report.is_partial
    assert report.blocked
    assert report.remaining_titles == []


def test_some_already_existing_is_reported_as_partial() -> None:
    report = jira.build_overlap_report(
        ["Create logout", "Create two factor authentication"], existing_login_work()
    )
    assert report.is_partial
    assert not report.is_full
    assert report.covered_titles == ["Create logout"]
    assert report.remaining_titles == ["Create two factor authentication"]


def test_nothing_existing_blocks_nothing() -> None:
    report = jira.build_overlap_report(["Create two factor authentication"], existing_login_work())
    assert not report.blocked
    assert report.covered_titles == []
    assert report.remaining_titles == ["Create two factor authentication"]


def test_the_report_carries_status_and_link_for_the_email() -> None:
    report = jira.build_overlap_report(
        ["Create logout"], existing_login_work(), base_url="https://x.atlassian.net"
    )
    match = report.matches[0]
    assert match.existing_status == "Done"
    assert match.existing_type == "Story"
    assert match.existing_url == "https://x.atlassian.net/browse/ORD-4"


# --- a request is not the work ----------------------------------------------
# Found live on TT2-164: the agent reported that "Record rest breaks" was
# already covered by TT2-164 — which WAS the ticket asking for it. A trigger
# ticket restates the requirement in its own description, so every Story
# derived from it matches it.


def request_ticket(key: str, summary: str, description: str) -> ExistingIssue:
    return ExistingIssue(
        key=key,
        summary=summary,
        issue_type="Task",
        labels=["ltw-awaiting-input"],
        description=description,
    )


def real_work(key: str, summary: str, description: str = "") -> ExistingIssue:
    return ExistingIssue(
        key=key,
        summary=summary,
        issue_type="Story",
        labels=["ltw-d444700f82ef14e4"],
        description=description,
    )


def test_the_trigger_ticket_never_matches_its_own_stories() -> None:
    existing = [
        request_ticket("TT2-164", "Driver rest breaks", "Allow drivers to record rest breaks")
    ]
    report = jira.build_overlap_report(["Record rest breaks"], existing, exclude_keys={"TT2-164"})
    assert not report.blocked
    assert report.remaining_titles == ["Record rest breaks"]


def test_other_request_tickets_are_not_treated_as_existing_work() -> None:
    """An older request is still a request, even after it has been processed."""
    existing = [
        ExistingIssue(
            key="TT2-137",
            summary="Driver rest break logging",
            issue_type="Task",
            labels=["ltw-processed"],
            description="Let drivers record the start and end of a rest break.",
        )
    ]
    assert jira.build_overlap_report(["Record a rest break"], existing).matches == []


def test_real_work_still_blocks() -> None:
    """The exclusions must not disarm duplicate detection itself."""
    existing = [real_work("TT2-40", "Track tyre replacement")]
    report = jira.build_overlap_report(["Track tyre replacement"], existing)
    assert report.blocked
    assert report.matches[0].existing_key == "TT2-40"


def test_a_request_ticket_is_recognised_by_either_marker() -> None:
    for marker in ("ltw-processed", "ltw-awaiting-input"):
        issue = ExistingIssue(key="TT2-1", summary="x", issue_type="Task", labels=[marker])
        assert jira.is_request_ticket(issue)
    assert not jira.is_request_ticket(
        ExistingIssue(key="TT2-2", summary="x", issue_type="Story", labels=["ltw-abc123"])
    )
    assert not jira.is_request_ticket(ExistingIssue(key="TT2-3", summary="x", issue_type="Story"))


def test_comparable_issues_drops_requests_subtasks_and_excluded_keys() -> None:
    existing = [
        request_ticket("TT2-164", "Driver rest breaks", "..."),
        real_work("TT2-40", "Track tyre replacement"),
        ExistingIssue(key="TT2-41", summary="Define the rules for: ...", issue_type="Sub-task"),
        real_work("TT2-50", "Excluded by key"),
    ]
    kept = jira.comparable_issues(existing, exclude_keys={"TT2-50"})
    assert [i.key for i in kept] == ["TT2-40"]


# --- validation judges the request, not its surroundings ---------------------
# Found live on TT2-175: "@Aetherion What is the weather today?" created a Story
# and three Sub-tasks. Validation was reading the whole assembled bundle — the
# request plus the ticket, its comments and its history — so a nearby comment
# saying "allow users to reset passwords" vouched for a request that could not
# stand on its own.

import asyncio  # noqa: E402

from src.context.ingest import assemble_requirement  # noqa: E402
from src.models.schemas import CommentInfo, SourceIssue  # noqa: E402
from src.tools.tools import validate_requirement  # noqa: E402

PROJECT = {
    "project": {
        "key": "TT2",
        "name": "FleetLink",
        "description": "A delivery fleet management platform.",
    },
    "existing_issues": [],
}


def bundle(request: str) -> str:
    """An assembled requirement with plenty of legitimate context around it."""
    source = SourceIssue(
        key="TT2-175",
        summary="Customer login",
        description="The login screen needs work before the next release.",
        trigger_comment_id="1",
        trigger_comment_author="Laxman",
        trigger_comment_body=request,
        comments=[
            CommentInfo(author="Ann", body="We should allow users to reset passwords too."),
            CommentInfo(author="Ravi", body="Let drivers update their status from the app."),
        ],
    )
    return assemble_requirement(source)[0]


def verdict_for(request: str) -> str:
    return asyncio.run(validate_requirement(bundle(request), PROJECT))["status"]


def test_an_out_of_scope_request_is_not_rescued_by_its_context() -> None:
    assert verdict_for("@Aetherion What is the weather today?") == "OUT_OF_SCOPE"


def test_gibberish_is_not_rescued_by_its_context() -> None:
    assert verdict_for("@Aetherion asdfgh qwerty zxcvbnm hjkl") == "VALIDATION_ERROR"


def test_an_empty_request_is_not_rescued_by_its_context() -> None:
    assert verdict_for("@Aetherion") == "VALIDATION_ERROR"


def test_a_real_request_still_passes_with_context_present() -> None:
    """The fix must not make the validator stricter about genuine requests."""
    assert (
        verdict_for("@Aetherion Allow drivers to record the start of a rest break.")
        == "READY_FOR_JIRA"
    )


def test_a_manual_run_without_sections_still_validates() -> None:
    """scripts/run_issue.py --text passes plain prose, with no headings."""
    plain = "Allow drivers to record the start of a rest break."
    assert asyncio.run(validate_requirement(plain, PROJECT))["status"] == "READY_FOR_JIRA"
    assert (
        asyncio.run(validate_requirement("What is the weather today?", PROJECT))["status"]
        == "OUT_OF_SCOPE"
    )
