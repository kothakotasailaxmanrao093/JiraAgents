"""Bug investigation mode (2026-10-22): code read, cause found, nothing invented.

A Bug was reviewed as if it were a requirement ("no acceptance criteria"), and
nothing looked at the code. In bug mode the agent reads the organisation's code
(read-only), and the model reports the cause, the affected files and a fix —
with every file it names checked against the files actually read.
"""

from __future__ import annotations

from typing import Any

from agent.delegated import run_delegated
from agent.review_flow import report_text, run_review
from review.code_terms import keywords, search_terms
from review.parser import parse_investigation
from tools.search_code_tool import content_score, is_code, path_score, search_code

BUG_SUMMARY = "Account balance is wrong after a successful transfer"
BUG_TEXT = (
    "POST /api/transfers succeeds but `TransferService` leaves the old balance. "
    'The log shows "Insufficient funds for account" and an IllegalStateException.'
)

# --- what to look for ---------------------------------------------------------


def test_search_terms_take_code_names_from_the_report() -> None:
    terms = search_terms(BUG_SUMMARY, BUG_TEXT)
    assert "TransferService" in terms
    assert "/api/transfers" in terms
    assert "IllegalStateException" in terms


def test_keywords_split_code_names_and_skip_filler_words() -> None:
    words = keywords(BUG_SUMMARY, BUG_TEXT)
    assert "transferservice" in words and "transfer" in words and "balance" in words
    for filler in ("successful", "wrong", "after", "shows"):
        assert filler not in words


def test_files_are_ranked_by_the_reports_words() -> None:
    words = ["transfer", "balance"]
    assert path_score("src/main/java/com/bank/TransferService.java", words) > path_score(
        "src/main/java/com/bank/Audit.java", words
    )
    assert content_score("balance = balance - amount; transfer(a, b)", words) > 0
    assert content_score("nothing relevant here", words) == 0


def test_only_code_files_are_considered() -> None:
    assert is_code("src/main/java/com/bank/TransferService.java")
    assert is_code("src/main/resources/application.yml")
    assert not is_code("docs/logo.png")
    assert not is_code("frontend/node_modules/lib/index.js")
    assert not is_code("package-lock.json")


async def test_no_github_access_is_a_note_not_a_failure(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_ORG", raising=False)
    monkeypatch.delenv("ORCH_GITHUB_ORG", raising=False)
    fn = getattr(search_code, "__wrapped__", search_code)
    out = await fn("BTS-30", BUG_SUMMARY, BUG_TEXT)
    assert out["documents"] == []
    assert "GitHub access is not configured" in out["note"]


def test_the_search_reads_what_the_person_wrote_not_the_bots_replies() -> None:
    docs = [
        {"kind": "requirement", "text": "the balance is wrong"},
        {"kind": "review_history", "text": "previous review questions"},
        {"kind": "source_code", "text": "class X {}"},
    ]
    text = report_text(docs)
    assert "balance is wrong" in text
    assert "previous review" not in text and "class X" not in text


# --- what the model says, checked ------------------------------------------------

CODE_DOCS = [
    {
        "kind": "source_code",
        "source_label": "Code Org/bank/src/TransferService.java",
        "metadata": {"repo": "Org/bank", "path": "src/TransferService.java"},
        "text": "class TransferService {}",
    }
]


def test_a_file_that_was_never_read_is_dropped() -> None:
    raw = {
        "investigation": {
            "root_cause": "The balance is read before the debit is saved.",
            "root_cause_confidence": "high",
            "affected_files": [
                {"path": "Org/bank/src/TransferService.java", "why": "saves late"},
                {"path": "src/TransferService.java", "why": "same file, no repo"},
                {"path": "Org/bank/src/Invented.java", "why": "never read"},
            ],
        }
    }
    found = parse_investigation(raw, CODE_DOCS)
    assert [f["path"] for f in found["affected_files"]] == ["Org/bank/src/TransferService.java"]
    assert found["files_dropped"] == 1
    assert found["root_cause_confidence"] == "high"


def test_high_confidence_needs_code_to_prove_it() -> None:
    raw = {"investigation": {"root_cause": "A race condition.", "root_cause_confidence": "high"}}
    assert parse_investigation(raw, documents=[])["root_cause_confidence"] == "medium"


def test_no_investigation_block_means_none() -> None:
    assert parse_investigation({"findings": []}, CODE_DOCS) == {}


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


def _responses(issue_type: str = "Bug") -> dict[str, Any]:
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
        "search_code": {
            "documents": CODE_DOCS,
            "terms": ["TransferService"],
            "searched": "transferservice, transfer, balance",
            "note": "",
        },
        "analyze_requirement": {
            "findings": [],
            "investigation": {
                "actual": "Old balance shown",
                "expected": "New balance shown",
                "root_cause": "The balance is read before the debit is saved.",
                "root_cause_confidence": "high",
                "affected_files": [{"path": "Org/bank/src/TransferService.java", "why": "x"}],
                "proposed_fix": "Save the debit first.",
                "missing_information": [],
            },
            "readiness": {"level": "needs_minor_clarification", "score": 4},
            "error": None,
        },
        "render_review": {
            "comment_markdown": "## draft",
            "comment_adf": {"type": "doc"},
            "by_category": {},
            "finding_count": 0,
        },
    }


async def test_a_bug_card_is_investigated_against_the_code() -> None:
    ex = FakeExecute(_responses("Bug"))
    out = await run_review({"issue_key": "BTS-30"}, ex)

    names = [n for n, _ in ex.calls]
    assert names.index("search_code") < names.index("analyze_requirement")
    documents, *_rest, review_kind = ex.args("analyze_requirement")
    assert review_kind == "bug"
    assert any(d["kind"] == "source_code" for d in documents)
    assert out[0]["review_kind"] == "bug"
    assert out[0]["investigation"]["code_note"].startswith("Code searched for:")
    assert "post_review_comment" not in names  # never posts


async def test_the_router_can_ask_for_bug_mode_on_any_card() -> None:
    ex = FakeExecute(_responses("Task"))
    await run_review({"issue_key": "BTS-30", "review_kind": "bug"}, ex)
    assert "search_code" in [n for n, _ in ex.calls]


async def test_a_story_review_is_unchanged() -> None:
    ex = FakeExecute(_responses("Story"))
    out = await run_review({"issue_key": "BTS-30"}, ex)
    names = [n for n, _ in ex.calls]
    assert "search_code" not in names
    assert ex.args("analyze_requirement")[-1] == "requirement"
    assert out[0]["review_kind"] == "requirement"


async def test_the_contract_carries_the_investigation_and_creates_nothing() -> None:
    ex = FakeExecute(_responses("Bug"))
    result = await run_delegated(
        {"issue_key": "BTS-30", "mode": "delegated", "review_kind": "bug", "run_id": "r1"},
        ex,
        run_review=run_review,
    )
    assert result["headline"] == "Bug investigation — likely cause found"
    assert result["investigation"]["proposed_fix"] == "Save the debit first."
    assert result["created"] == []
    assert any("TransferService.java" in s for s in result["sources_read"])


# --- BTS-31 (2026-10-23): judged on the report, not on the agent's access ----------


def test_search_terms_ignore_the_agent_and_its_mentions() -> None:
    docs = [
        {"kind": "trigger_comment", "text": "@Aetherion investigate"},
        {
            "kind": "target_comments",
            "text": "AetherionAgent · Nothing was created\n— automated reply",
        },
        {"kind": "requirement", "text": "approveDeferredReport is never sent"},
    ]
    text = report_text(docs)
    assert "approveDeferredReport" in text
    assert "Aetherion" not in text and "automated" not in text


def test_the_bug_prompt_scores_the_report_not_the_code_access() -> None:
    from review.prompt import BUG_SYSTEM_PROMPT

    assert "Whether you could see the code does NOT change readiness" in BUG_SYSTEM_PROMPT
    assert "never \\\nput it, or a request for file names" not in BUG_SYSTEM_PROMPT  # wrapped text
    assert "request for file names, paths, frameworks or code" in BUG_SYSTEM_PROMPT.replace(
        "\n", " "
    )


async def test_unrelated_code_is_reported_as_not_found_not_asked_for() -> None:
    responses = _responses("Bug")
    responses["analyze_requirement"] = {
        **responses["analyze_requirement"],
        "investigation": {
            **responses["analyze_requirement"]["investigation"],
            "affected_files": [],
        },
    }
    ex = FakeExecute(responses)
    out = await run_review({"issue_key": "BTS-31"}, ex)
    note = out[0]["investigation"]["code_note"]
    assert note.startswith("No file read from the repositories this agent can access")
    assert "comes from the report" in note


def test_the_reporter_is_never_asked_for_code() -> None:
    raw = {
        "investigation": {
            "root_cause": "Untrimmed phone input fails the anchored regex.",
            "missing_information": [
                "Exact client-side code (form library, validation function and submit handler).",
                "Whether other fields use the same internationalPhoneNumber regex.",
            ],
        }
    }
    found = parse_investigation(raw, [])
    assert found["missing_information"] == [
        "Whether other fields use the same internationalPhoneNumber regex."
    ]
