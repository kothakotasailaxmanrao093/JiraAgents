"""A review the router asks to use the card's memory (2026-10-30): the memory is
read before the analysis and shown to it as a source, kept after it, and handed
back in the result. Without ``memory_repos`` nothing changes."""

from __future__ import annotations

from agent.delegated import run_delegated
from agent.review_flow import run_review

MEMORY_DOC = {
    "source_id": "card_memory",
    "source_label": "Earlier answers and related work",
    "kind": "card_memory",
    "text": "Earlier cards about the same thing:\n- BTS-20 NEFT remark",
}
SERVICES = [{"service": "payments-service", "rank": 8, "reason": "BTS-20 PR #41", "prs": []}]


class Execute:
    def __init__(self, recall_ok: bool = True):
        self.calls: list[tuple[str, tuple]] = []
        self.recall_ok = recall_ok

    async def __call__(self, name, *args, **_):
        self.calls.append((name, args))
        if name == "gather_context":
            return {
                "error": None,
                "has_requirement": True,
                "issue_summary": "Add a remark to IMPS payments",
                "documents": [
                    {"kind": "requirement", "source_label": "Target (description)", "text": "Add"},
                    {"kind": "attachment", "source_label": "spec.pdf", "text": "POST /imps"},
                ],
                "target_bundle": {
                    "comments": [
                        {"id": "1", "body": "Yes, same as NEFT."},
                        {"id": "2", "body": "AetherionAgent · Requirement review ..."},
                    ]
                },
                "warnings": [],
            }
        if name == "recall_card_memory":
            if not self.recall_ok:
                return {"ok": False, "error": "MemoryUnavailable: not set"}
            return {"ok": True, "memory": {"seq": 1, "services": SERVICES}, "document": MEMORY_DOC}
        if name == "analyze_requirement":
            return {
                "findings": [
                    {
                        "finding_type": "Open Question",
                        "description": "Should the SMS include the remark?",
                        "evidence_source": "Target (description)",
                    }
                ],
                "readiness": {"level": "needs_minor_clarification", "score": 4},
                "error": None,
            }
        if name == "render_review":
            return {"comment_markdown": "## draft", "finding_count": 1, "by_category": {}}
        if name == "keep_card_memory":
            return {
                "ok": True,
                "services": SERVICES,
                "questions": [{"question": "Should the SMS include the remark?", "answers": []}],
            }
        raise AssertionError(name)

    def args(self, name):
        return next(args for called, args in self.calls if called == name)


async def test_the_review_reads_its_memory_first_and_keeps_its_questions():
    ex = Execute()
    out = await run_delegated(
        {"issue_key": "BTS-60", "memory_repos": "payments-service: org/payments"},
        ex,
        run_review=run_review,
    )
    names = [name for name, _ in ex.calls]
    assert names.index("recall_card_memory") < names.index("analyze_requirement")
    assert names.index("keep_card_memory") > names.index("analyze_requirement")
    key, title, text, comments, repos = ex.args("recall_card_memory")
    assert (key, title, repos) == (
        "BTS-60",
        "Add a remark to IMPS payments",
        "payments-service: org/payments",
    )
    assert "=== spec.pdf ===" in text
    assert comments == [{"id": "1", "text": "Yes, same as NEFT."}]
    assert MEMORY_DOC in ex.args("analyze_requirement")[0]
    assert ex.args("keep_card_memory")[2] == ["Should the SMS include the remark?"]
    assert out["memory"]["services"] == SERVICES
    assert out["memory"]["questions"][0]["answers"] == []


async def test_without_memory_repos_the_review_is_as_before():
    ex = Execute()
    out = await run_delegated({"issue_key": "BTS-60"}, ex, run_review=run_review)
    names = [name for name, _ in ex.calls]
    assert "recall_card_memory" not in names and "keep_card_memory" not in names
    assert out["memory"] == {}


async def test_memsy_not_answering_never_stops_the_review():
    ex = Execute(recall_ok=False)
    out = await run_delegated(
        {"issue_key": "BTS-60", "memory_repos": ""}, ex, run_review=run_review
    )
    names = [name for name, _ in ex.calls]
    assert "keep_card_memory" not in names
    assert out["outcome"] == "REVIEWED" and out["memory"] == {}
    assert MEMORY_DOC not in ex.args("analyze_requirement")[0]
