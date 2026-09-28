"""Post orchestration — fully faked tools, no SDK, no network."""

from __future__ import annotations

from agent.post_flow import run_post


class FakeExecute:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[tuple] = []

    async def __call__(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return self.response


async def test_post_happy_path():
    ex = FakeExecute({"posted": True, "comment_id": "100", "comment_url": "u", "error": None})
    out = await run_post({"issue_key": "ABC-1", "comment_markdown": "approved text"}, ex)
    assert out[0]["status"] == "success"
    assert out[0]["comment_id"] == "100"
    assert ex.calls[0][0] == "post_review_comment"


async def test_post_requires_content():
    ex = FakeExecute({})
    out = await run_post({"issue_key": "ABC-1"}, ex)
    assert out[0]["status"] == "error"
    assert out[0]["error"] == "no_content"
    assert ex.calls == []


async def test_post_missing_issue_key():
    ex = FakeExecute({})
    out = await run_post({"comment_markdown": "x"}, ex)
    assert out[0]["status"] == "error"
    assert ex.calls == []


async def test_post_failure_degrades():
    ex = FakeExecute({"posted": False, "error": "403 Forbidden"})
    out = await run_post(
        {"issue_key": "ABC-1", "findings": [{"finding_type": "Assumption", "description": "x"}]}, ex
    )
    assert out[0]["status"] == "completed_with_warnings"
    assert "403" in out[0]["message"]


async def test_post_accepts_findings_when_no_markdown():
    ex = FakeExecute({"posted": True, "comment_id": "7", "comment_url": "u", "error": None})
    out = await run_post(
        {"issue_key": "ABC-1", "findings": [{"finding_type": "Assumption", "description": "x"}]}, ex
    )
    assert out[0]["status"] == "success"
    # the findings were forwarded to the tool
    _, args, _ = ex.calls[0]
    assert args[0] == "ABC-1"
