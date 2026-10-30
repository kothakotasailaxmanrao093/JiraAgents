"""A review given the card's memory by the router (2026-10-30): earlier
answers, earlier cards with their PRs, decisions in force and ranked services
arrive as one more source the analysis reads. The review keeps nothing."""

from __future__ import annotations

from agent.delegated import run_delegated
from agent.review_flow import run_review

MEMORY_DOC = {
    "source_id": "card_memory",
    "source_label": "Earlier answers and related work",
    "kind": "card_memory",
    "text": "Earlier cards about the same thing:\n- BTS-20 NEFT remark",
}


class Execute:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def __call__(self, name, *args, **_):
        self.calls.append((name, args))
        if name == "gather_context":
            return {
                "error": None,
                "has_requirement": True,
                "issue_summary": "Add a remark to IMPS payments",
                "documents": [{"kind": "requirement", "source_label": "Target (description)"}],
                "target_bundle": {},
                "warnings": [],
            }
        if name == "analyze_requirement":
            return {
                "findings": [
                    {
                        "finding_type": "Open Question",
                        "description": "Should the SMS include the remark?",
                        "evidence_source": "Earlier answers and related work",
                    }
                ],
                "readiness": {"level": "needs_minor_clarification", "score": 4},
                "error": None,
            }
        if name == "render_review":
            return {"comment_markdown": "## draft", "finding_count": 1, "by_category": {}}
        raise AssertionError(f"the review called {name}")

    def args(self, name):
        return next(args for called, args in self.calls if called == name)


async def test_the_memory_is_read_as_one_more_source():
    ex = Execute()
    out = await run_delegated(
        {"issue_key": "BTS-60", "memory_document": MEMORY_DOC}, ex, run_review=run_review
    )
    assert MEMORY_DOC in ex.args("analyze_requirement")[0]
    assert [name for name, _ in ex.calls] == [
        "gather_context",
        "analyze_requirement",
        "render_review",
    ]
    assert out["questions"] == ["Should the SMS include the remark?"]


async def test_without_memory_the_review_is_as_before():
    ex = Execute()
    await run_delegated({"issue_key": "BTS-60"}, ex, run_review=run_review)
    assert all(d.get("kind") != "card_memory" for d in ex.args("analyze_requirement")[0])
