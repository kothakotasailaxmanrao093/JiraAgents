"""A bug's REPORT is reviewed; the code is not (2026-10-26).

Requirement Review finds gaps in what people wrote. For a Bug it judges whether
the report can be investigated (steps, actual, expected, trigger) and lists only
what the reporter can still supply. The root cause in the code, the affected
files and the fix come from the Planning agent, which holds the code graph.
"""

from __future__ import annotations

from typing import Any

from agent.delegated import run_delegated
from agent.review_flow import bug_report_markdown, questions_from, run_review
from review.parser import parse_investigation
from review.prompt import BUG_SYSTEM_PROMPT

BUG_SUMMARY = "Account balance is wrong after a successful transfer"
BUG_TEXT = (
    "POST /api/transfers succeeds but `TransferService` leaves the old balance. "
    'The log shows "Insufficient funds for account" and an IllegalStateException.'
)

# --- what the model says, checked ------------------------------------------------


def test_the_report_sections_are_kept() -> None:
    raw = {
        "investigation": {
            "actual": "Old balance shown",
            "expected": "New balance shown",
            "suspected_cause_from_report": "The report says the debit is saved late.",
            "missing_information": ["Which account type shows it"],
        }
    }
    assert parse_investigation(raw) == {
        "actual": "Old balance shown",
        "expected": "New balance shown",
        "suspected_cause_from_report": "The report says the debit is saved late.",
        "missing_information": ["Which account type shows it"],
    }


def test_code_fields_from_the_model_are_not_passed_on() -> None:
    raw = {
        "investigation": {
            "actual": "Old balance shown",
            "root_cause": "A guess about the code",
            "affected_files": [{"path": "src/X.java"}],
            "proposed_fix": "Change X",
        }
    }
    found = parse_investigation(raw)
    assert set(found) == {
        "actual",
        "expected",
        "suspected_cause_from_report",
        "missing_information",
    }


def test_no_investigation_block_means_none() -> None:
    assert parse_investigation({"findings": []}) == {}


def test_the_reporter_is_never_asked_for_code() -> None:
    raw = {
        "investigation": {
            "actual": "Submit does nothing",
            "missing_information": [
                "Exact client-side code (form library, validation function and submit handler).",
                "Whether other fields use the same internationalPhoneNumber regex.",
            ],
        }
    }
    assert parse_investigation(raw)["missing_information"] == [
        "Whether other fields use the same internationalPhoneNumber regex."
    ]


def test_the_bug_prompt_judges_the_report_and_never_asks_for_code() -> None:
    prompt = BUG_SYSTEM_PROMPT.replace("\n", " ")
    assert "You do not see the code and must not ask for it" in prompt
    assert "Never ask for code, file names, paths, frameworks or libraries" in prompt
    assert '"root_cause"' not in prompt and '"affected_files"' not in prompt


# --- the flow ------------------------------------------------------------------


class FakeExecute:
    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[tuple[str, tuple]] = []

    async def __call__(self, name, *args, **kwargs):
        self.calls.append((name, args))
        return self.responses[name]

    def args(self, name: str) -> tuple:
        return next(a for n, a in self.calls if n == name)


def _responses(issue_type: str = "Bug", score: int = 5) -> dict[str, Any]:
    return {
        "gather_context": {
            "error": None,
            "has_requirement": True,
            "issue_summary": BUG_SUMMARY,
            "documents": [
                {
                    "kind": "requirement",
                    "source_label": "Target BTS-30 (description)",
                    "text": BUG_TEXT,
                }
            ],
            "target_bundle": {"issue_type": issue_type, "description_text": BUG_TEXT},
            "warnings": [],
        },
        "analyze_requirement": {
            "findings": [],
            "investigation": {
                "actual": "Old balance shown",
                "expected": "New balance shown",
                "suspected_cause_from_report": "",
                "missing_information": [],
            },
            "readiness": {"level": "ready", "score": score},
            "error": None,
        },
        "render_review": {
            "comment_markdown": "## draft",
            "comment_adf": {"type": "doc"},
            "by_category": {},
            "finding_count": 0,
        },
    }


async def test_a_bug_card_gets_a_report_review_and_no_code_search() -> None:
    ex = FakeExecute(_responses("Bug"))
    out = await run_review({"issue_key": "BTS-30"}, ex)

    names = [n for n, _ in ex.calls]
    assert "search_code" not in names
    assert ex.args("analyze_requirement")[-1] == "bug"
    assert out[0]["review_kind"] == "bug"
    assert out[0]["investigation"]["actual"] == "Old balance shown"
    assert "post_review_comment" not in names  # never posts


async def test_the_router_can_ask_for_bug_mode_on_any_card() -> None:
    ex = FakeExecute(_responses("Task"))
    await run_review({"issue_key": "BTS-30", "review_kind": "bug"}, ex)
    assert ex.args("analyze_requirement")[-1] == "bug"


async def test_a_story_review_is_unchanged() -> None:
    ex = FakeExecute(_responses("Story"))
    out = await run_review({"issue_key": "BTS-30"}, ex)
    assert ex.args("analyze_requirement")[-1] == "requirement"
    assert out[0]["review_kind"] == "requirement"
    assert out[0]["comment_markdown"] == "## draft"


async def test_a_direct_bug_review_shows_the_report_sections() -> None:
    ex = FakeExecute(_responses("Bug"))
    out = await run_review({"issue_key": "BTS-30"}, ex)
    draft = out[0]["comment_markdown"]
    assert draft.startswith("### Bug report")
    assert "**Actual:** Old balance shown" in draft and "**Expected:** New balance shown" in draft
    assert draft.endswith("## draft")


def test_the_report_markdown_lists_what_is_still_needed() -> None:
    text = bug_report_markdown({"actual": "a", "missing_information": ["Which browser"]})
    assert "**Information still needed:**\n- Which browser" in text


async def test_a_ready_report_is_ready_to_investigate_and_creates_nothing() -> None:
    ex = FakeExecute(_responses("Bug", score=5))
    result = await run_delegated(
        {"issue_key": "BTS-30", "mode": "delegated", "review_kind": "bug", "run_id": "r1"},
        ex,
        run_review=run_review,
    )
    assert result["outcome"] == "REVIEWED"
    assert result["headline"] == "Bug report reviewed — ready to investigate"
    assert result["investigation"]["expected"] == "New balance shown"
    assert result["created"] == []


async def test_a_thin_report_needs_more_information() -> None:
    ex = FakeExecute(_responses("Bug", score=2))
    result = await run_delegated(
        {"issue_key": "BTS-30", "mode": "delegated", "review_kind": "bug", "run_id": "r1"},
        ex,
        run_review=run_review,
    )
    assert result["headline"] == "Bug report reviewed — more information needed"


# --- known bugs fixed 2026-10-26 ---------------------------------------------------


def test_open_questions_are_found_by_finding_type() -> None:
    findings = [
        {"finding_type": "Open Question", "description": "Per account or per customer?"},
        {"finding_type": "Dependency Question", "description": "Which team owns the ledger?"},
        {"finding_type": "Requirement Gap", "description": "No error message given."},
    ]
    assert questions_from(findings) == [
        "Per account or per customer?",
        "Which team owns the ledger?",
    ]


async def test_a_failed_ai_step_is_not_reported_as_a_review() -> None:
    responses = _responses("Story")
    responses["analyze_requirement"] = {"findings": [], "readiness": None, "error": "timeout"}
    result = await run_delegated(
        {"issue_key": "BTS-30", "mode": "delegated", "run_id": "r1"},
        FakeExecute(responses),
        run_review=run_review,
    )
    assert result["outcome"] == "FAILED"  # never kept and reused as a review
    assert "timeout" in result["summary"] and "Ask again" in result["summary"]
