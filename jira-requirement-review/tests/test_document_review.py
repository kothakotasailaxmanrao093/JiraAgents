"""Reviewing documents with no Jira ticket (2026-10-08).

A request started on the orchestrator's form writes nothing to Jira: Task
Creation's breakdown PDF, with the files the person attached, is what Review
scores — and the score decides whether Planning starts. On Review's own form,
files uploaded with no Issue Key and no query are reviewed the same way.
"""

from __future__ import annotations

from agent.delegated import run_delegated
from agent.review_flow import resolve_document_keys, run_review
from shared.contract import Outcome
from tools import gather_documents_tool
from tools.gather_documents_tool import as_review_documents

PDF = "breakdowns/20261008T101500Z_aetherion-breakdown-BGV-REQUEST.pdf"
SPEC = "local-uploads/ab12/daily-limit-spec.docx"


class FakeExecute:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def __call__(self, name, *args, **kwargs):
        self.calls.append((name, args))
        return {
            "gather_documents": {
                "has_requirement": True,
                "issue_summary": "Daily transfer limit",
                "documents": [{"kind": "requirement", "source_label": f"Document: {PDF}"}],
                "target_bundle": {"key": "Daily transfer limit", "document_review": True},
                "warnings": [],
            },
            "analyze_requirement": {
                "findings": [
                    {
                        "category": "Missing_Acceptance_Criteria",
                        "description": "What happens at exactly the limit?",
                        "evidence_source": f"Document: {PDF}",
                        "confidence": "high",
                    }
                ],
                "readiness": {"level": "Mostly Ready", "score": 4},
                "error": None,
            },
            "render_review": {"comment_markdown": "## draft", "finding_count": 1},
            "post_review_comment": {"posted": True},
        }[name]

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


async def test_documents_are_reviewed_without_reading_jira() -> None:
    ex = FakeExecute()
    out = await run_review(
        {"document_file_keys": [PDF, SPEC], "document_title": "Daily transfer limit"}, ex
    )
    assert ex.names() == ["gather_documents", "analyze_requirement", "render_review"]
    assert ex.calls[0][1][:2] == ([PDF, SPEC], "Daily transfer limit")
    assert out[0]["issue_key"] == "Daily transfer limit" and out[0]["finding_count"] == 1


async def test_a_document_review_never_posts_to_jira() -> None:
    ex = FakeExecute()
    await run_review({"document_file_keys": [PDF], "post_to_jira": True}, ex)
    assert "post_review_comment" not in ex.names()


async def test_files_alone_on_the_form_are_the_documents() -> None:
    ex = FakeExecute()
    await run_review({"transcript_file": [SPEC]}, ex)
    assert ex.names()[0] == "gather_documents"


def test_with_a_ticket_uploads_stay_context_for_that_ticket() -> None:
    assert resolve_document_keys({"issue_key": "BGV-1", "transcript_file": [SPEC]}) == []
    assert resolve_document_keys({"document_file_keys": f"{PDF}, {SPEC}"}) == [PDF, SPEC]


async def test_the_orchestrator_gets_a_scored_review_of_the_documents() -> None:
    ex = FakeExecute()
    out = await run_delegated(
        {"mode": "delegated", "document_file_keys": [PDF], "document_title": "Daily limit"},
        ex,
        run_review=run_review,
    )
    assert out["outcome"] == Outcome.REVIEWED.value
    assert out["readiness_score"] == 4
    assert out["findings"][0]["description"] == "What happens at exactly the limit?"


async def test_nothing_to_review_is_said() -> None:
    out = await run_delegated({"mode": "delegated"}, FakeExecute(), run_review=run_review)
    assert out["outcome"] == Outcome.FAILED.value
    assert "issue key or document" in out["summary"]


def test_files_are_labelled_as_the_requirement() -> None:
    docs = as_review_documents(
        [
            {"kind": "transcript", "text": "x", "metadata": {"file_key": PDF}},
            {"kind": "transcript", "text": "y", "metadata": {"file_key": "u/standup.vtt"}},
        ]
    )
    assert docs[0]["source_label"] == f"Document: {PDF.rsplit('/', 1)[1]}"
    assert docs[0]["kind"] == "requirement" and docs[1]["kind"] == "transcript"


async def test_the_tool_reads_the_files_from_storage(monkeypatch) -> None:
    async def load(self, ctx):
        from context.base import ContextDocument

        return [
            ContextDocument("transcript", "x", "transcript", f"text of {k}", {"file_key": k})
            for k in ctx.transcript_file_keys
        ]

    monkeypatch.setenv("TENANT_ID", "team")
    monkeypatch.setattr(gather_documents_tool.TranscriptSource, "load", load)
    out = await gather_documents_tool.gather_documents([PDF, SPEC], "Daily limit")
    assert out["has_requirement"] is True and out["issue_summary"] == "Daily limit"
    assert [d["source_label"] for d in out["documents"]] == [
        f"Document: {PDF.rsplit('/', 1)[1]}",
        "Document: daily-limit-spec.docx",
    ]


async def test_the_previous_reviews_questions_are_read_with_the_ticket() -> None:
    """2026-10-08: answered in the comments, asked again — only what is still open."""
    seen: dict = {}

    class Ex(FakeExecute):
        async def __call__(self, name, *args, **kwargs):
            if name == "gather_context":
                return {
                    "has_requirement": True,
                    "documents": [{"kind": "requirement", "text": "x"}],
                    "warnings": [],
                }
            if name == "analyze_requirement":
                seen["documents"] = args[0]
            return await super().__call__(name, *args, **kwargs)

    await run_review({"issue_key": "BGV-1", "previous_questions": ["Per account?"]}, Ex())
    last = seen["documents"][-1]
    assert last["source_label"] == "Previous review's open questions"
    assert "- Per account?" in last["text"] and "do not raise it again" in last["text"]


def test_acceptance_criteria_answered_in_a_comment_count() -> None:
    """BGV-106 (2026-10-08): the criteria came in a comment; "has none" was wrong."""
    from review.completeness import sources_missing

    target = {"key": "BGV-106", "description_text": "Limits per customer type."}
    without = sources_missing(target, [], [])
    with_comment = sources_missing(
        target,
        [{"kind": "target_comments", "text": "- Priya: Acceptance criteria: (1) 10000 OK …"}],
        [],
    )
    assert any("no acceptance criteria" in m.what for m in without)
    assert not any("no acceptance criteria" in m.what for m in with_comment)
