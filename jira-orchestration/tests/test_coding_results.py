"""Coding's answer since its rewrite (2026-11-23): the platform's ``results``
links, not ``pull_requests``. The shape below is exactly what
coding-agent/src/agent/agent.py ``_answer`` returns."""

from __future__ import annotations

from routing import pipeline

NEW_CODING = {
    "status": "success",
    "results": [
        {
            "title": "Draft PR: ui",
            "link": "[Open the pull request for ui](https://github.asurint.com/keystone/ui/pull/12)",
            "label": "Open PR for ui",
            "extension": "url",
        },
        {
            "title": "Branch fix/ks-6-123: api",
            "link": "[Open the branch for api](https://github.asurint.com/keystone/api/tree/fix/ks-6-123)",
            "label": "Open branch for api",
            "extension": "url",
        },
    ],
}


def test_a_pull_request_in_the_new_results_is_read() -> None:
    assert pipeline.pull_requests(NEW_CODING) == [
        {"service": "ui", "pr_url": "https://github.asurint.com/keystone/ui/pull/12", "draft": True}
    ]


def test_a_branch_without_a_pull_request_is_not_a_pull_request() -> None:
    assert pipeline.pushed_branches(NEW_CODING) == [
        {
            "service": "api",
            "branch": "fix/ks-6-123",
            "url": "https://github.asurint.com/keystone/api/tree/fix/ks-6-123",
        }
    ]


def test_the_old_shape_still_reads() -> None:
    old = {"pull_requests": [{"service": "bank", "pr_url": "https://x/pull/1"}]}
    assert pipeline.pull_requests(old) == old["pull_requests"]


def test_pr_lines_name_a_draft_from_the_new_shape() -> None:
    [line] = pipeline.pr_lines(NEW_CODING)
    assert line.startswith("ui: https://github.asurint.com/keystone/ui/pull/12")
