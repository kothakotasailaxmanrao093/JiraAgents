"""Review (draft) orchestration — fully faked tools, no SDK, no network."""

from __future__ import annotations

from agent.review_flow import run_review


class FakeExecute:
    """Dispatches execute(name, ...) to canned responses; records calls."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    async def __call__(self, name, *args, **kwargs):
        self.calls.append(name)
        return self.responses[name]


async def test_happy_path_returns_draft_and_never_posts():
    ex = FakeExecute(
        {
            "gather_context": {
                "error": None,
                "has_requirement": True,
                "issue_summary": "Sum",
                "documents": [
                    {"kind": "requirement", "source_label": "Target ABC-1 (description)"}
                ],
                "warnings": [],
            },
            "analyze_requirement": {
                "findings": [
                    {
                        "finding_type": "Assumption",
                        "description": "x",
                        "evidence_source": "Target ABC-1 (description)",
                        "confidence": "high",
                    }
                ],
                "error": None,
            },
            "render_review": {
                "comment_markdown": "## draft",
                "comment_adf": {"type": "doc"},
                "by_category": {"Assumption": 1},
                "finding_count": 1,
            },
        }
    )
    out = await run_review({"issue_key": "ABC-1"}, ex)
    assert out[0]["status"] == "success"
    assert out[0]["finding_count"] == 1
    assert out[0]["comment_markdown"] == "## draft"
    assert "post_review_comment" not in ex.calls  # NEVER posts


async def test_missing_issue_key_is_fatal_and_calls_nothing():
    ex = FakeExecute({})
    out = await run_review({}, ex)
    assert out[0]["status"] == "error"
    assert ex.calls == []


async def test_no_requirement_text_is_fatal():
    ex = FakeExecute({"gather_context": {"error": None, "has_requirement": False, "warnings": []}})
    out = await run_review({"issue_key": "ABC-1"}, ex)
    assert out[0]["status"] == "error"
    assert out[0]["error"] == "no_requirement_text"


async def test_gather_error_is_fatal():
    ex = FakeExecute({"gather_context": {"error": "boom", "has_requirement": False}})
    out = await run_review({"issue_key": "ABC-1"}, ex)
    assert out[0]["status"] == "error"
    assert "boom" in out[0]["message"]


async def test_llm_error_degrades_without_draft():
    ex = FakeExecute(
        {
            "gather_context": {
                "error": None,
                "has_requirement": True,
                "documents": [{"kind": "requirement"}],
                "warnings": ["parent skipped"],
            },
            "analyze_requirement": {"findings": [], "error": "rate limited"},
        }
    )
    out = await run_review({"issue_key": "ABC-1"}, ex)
    assert out[0]["status"] == "completed_with_warnings"
    assert out[0]["analysis_error"] == "rate limited"
    assert "render_review" not in ex.calls


def test_resolve_transcript_file_key_variants():
    from agent.review_flow import resolve_transcript_file_key

    assert resolve_transcript_file_key({"uploaded_files": "a.txt"}) == "a.txt"
    assert resolve_transcript_file_key({"transcript_file": "b.txt"}) == "b.txt"
    assert resolve_transcript_file_key({"transcript_file_key": "c.txt"}) == "c.txt"
    assert resolve_transcript_file_key({"attachments": ["d.txt", "e.txt"]}) == "d.txt"
    assert resolve_transcript_file_key({}) is None
    assert resolve_transcript_file_key({"attachments": []}) is None
    # precedence: uploaded_files wins over the trigger name
    assert resolve_transcript_file_key({"uploaded_files": "u", "transcript_file": "t"}) == "u"


def _ex_with_post(post_response):
    return FakeExecute(
        {
            "gather_context": {
                "error": None,
                "has_requirement": True,
                "issue_summary": "S",
                "documents": [{"kind": "requirement"}],
                "warnings": [],
            },
            "analyze_requirement": {
                "findings": [
                    {"finding_type": "Assumption", "description": "x", "confidence": "high"}
                ],
                "error": None,
            },
            "render_review": {
                "comment_markdown": "## draft",
                "comment_adf": {"type": "doc"},
                "by_category": {"Assumption": 1},
                "finding_count": 1,
            },
            "post_review_comment": post_response,
        }
    )


async def test_post_to_jira_true_posts_directly():
    ex = _ex_with_post(
        {"posted": True, "comment_id": "100", "comment_url": "http://j/c/100", "error": None}
    )
    out = await run_review({"issue_key": "ABC-1", "post_to_jira": True}, ex)
    assert out[0]["status"] == "success"
    assert out[0]["posted"] is True
    assert out[0]["comment_id"] == "100"
    assert "post_review_comment" in ex.calls


async def test_post_to_jira_failure_falls_back_to_draft():
    ex = _ex_with_post({"posted": False, "error": "403 Forbidden"})
    out = await run_review({"issue_key": "ABC-1", "post_to_jira": True}, ex)
    assert out[0]["status"] == "completed_with_warnings"
    assert out[0]["posted"] is False
    assert out[0]["post_error"] == "403 Forbidden"
    assert out[0]["comment_markdown"] == "## draft"  # draft preserved for manual paste


async def test_default_is_draft_only_and_never_posts():
    ex = _ex_with_post({"posted": True, "comment_id": "x"})
    out = await run_review({"issue_key": "ABC-1"}, ex)  # post_to_jira defaults False
    assert out[0]["status"] == "success"
    assert out[0]["posted"] is False
    assert out[0]["comment_markdown"] == "## draft"
    assert "post_review_comment" not in ex.calls


def _single_issue_responses(finding_desc: str = "x"):
    return {
        "gather_context": {
            "error": None,
            "has_requirement": True,
            "issue_summary": "Sum",
            "documents": [{"kind": "requirement", "source_label": "Target ABC-1 (description)"}],
            "warnings": [],
        },
        "analyze_requirement": {
            "findings": [
                {
                    "finding_type": "Assumption",
                    "description": finding_desc,
                    "evidence_source": "Target ABC-1 (description)",
                    "confidence": "high",
                }
            ],
            "readiness": {"level": "ready", "score": 5, "executive_summary": "Clear."},
            "error": None,
        },
        "render_review": {
            "comment_markdown": "## draft",
            "comment_adf": {"type": "doc"},
            "by_category": {"Assumption": 1},
            "finding_count": 1,
        },
    }


# === Batch (JQL) mode ===================================================


async def test_batch_mode_reviews_every_matched_issue():
    ex = FakeExecute(
        {
            "jira_search": {"issue_keys": ["ABC-1", "ABC-2"], "total": 2, "error": None},
            **_single_issue_responses(),
        }
    )
    out = await run_review({"jql_override": 'sprint = "S1"'}, ex)
    assert len(out) == 2
    assert all(r["status"] == "success" for r in out)
    assert ex.calls.count("gather_context") == 2
    assert ex.calls.count("analyze_requirement") == 2


async def test_batch_mode_no_matches_is_error():
    ex = FakeExecute({"jira_search": {"issue_keys": [], "total": 0, "error": None}})
    out = await run_review({"jql_override": "project = X"}, ex)
    assert out[0]["status"] == "error"
    assert out[0]["error"] == "no_issues_matched"
    assert "gather_context" not in ex.calls


async def test_batch_mode_search_error_is_fatal():
    ex = FakeExecute({"jira_search": {"issue_keys": [], "total": 0, "error": "bad jql"}})
    out = await run_review({"jql_override": "???"}, ex)
    assert out[0]["status"] == "error"
    assert "bad jql" in out[0]["message"]


async def test_neither_issue_key_nor_jql_is_fatal():
    ex = FakeExecute({})
    out = await run_review({}, ex)
    assert out[0]["status"] == "error"
    assert ex.calls == []


# === Word report / email delivery =======================================


async def test_generate_docx_appends_download_link_and_summary():
    ex = FakeExecute(
        {
            **_single_issue_responses(),
            "build_review_report": {
                "s3_key": "RequirementReviewReports/ABC-1_2026-01-01.docx",
                "extension": "docx",
                "metrics": {"issue_count": 1, "total_findings": 1, "overall_readiness": "ready"},
                "email_html": "<p>hi</p>",
                "error": None,
            },
        }
    )
    out = await run_review({"issue_key": "ABC-1", "generate_docx": True}, ex)
    assert out[0]["status"] == "success"  # primary per-issue result stays at index 0
    assert out[1]["type"] == "s3_download_link"
    assert out[1]["file_key"].endswith(".docx")
    assert out[2]["mode"] == "single"
    assert out[2]["issue_count"] == 1
    assert "build_review_report" in ex.calls
    assert "send_review_report_email" not in ex.calls  # send_email not requested


async def test_no_report_calls_when_docx_and_email_both_off():
    ex = FakeExecute(_single_issue_responses())
    out = await run_review({"issue_key": "ABC-1"}, ex)
    assert len(out) == 1
    assert "build_review_report" not in ex.calls


async def test_send_email_is_temporarily_disabled_even_when_requested():
    # send_email is force-disabled regardless of payload — no metadata.json trigger
    # exposes it, and the flow ignores the value even if a caller sends it directly.
    ex = FakeExecute(
        {
            **_single_issue_responses(),
            "build_review_report": {
                "s3_key": "k.docx",
                "extension": "docx",
                "metrics": {"total_findings": 1, "overall_readiness": "ready"},
                "email_html": "<p>hi</p>",
                "error": None,
            },
        }
    )
    out = await run_review({"issue_key": "ABC-1", "generate_docx": True, "send_email": True}, ex)
    assert "send_review_report_email" not in ex.calls
    assert out[-1]["email_status"] == "skipped"
    assert out[0]["status"] == "success"


# === Flag threading =======================================================


async def test_include_linked_issues_threaded_to_gather_context():
    captured = {}

    class CapturingExecute(FakeExecute):
        async def __call__(self, name, *args, **kwargs):
            if name == "gather_context":
                captured["args"] = args
            return await super().__call__(name, *args, **kwargs)

    ex = CapturingExecute(_single_issue_responses())
    await run_review({"issue_key": "ABC-1", "include_linked_issues": False}, ex)
    # gather_context(issue_key, include_parent, include_subtasks, include_linked_issues, ...)
    assert captured["args"][0] == "ABC-1"
    assert captured["args"][3] is False
