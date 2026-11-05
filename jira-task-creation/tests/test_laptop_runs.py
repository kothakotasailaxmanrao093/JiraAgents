"""Laptop runs, the "Reply on the ticket" choice, and the PDF despite duplicates.

2026-10-06, testing on a colleague's real Jira from a laptop: a run with an
Issue Key waited for an "@Aetherion" comment, then replied on their ticket; and
a PDF requested for KS-17185 never came because part of the work already
existed. Pinned here:

* a laptop run with an Issue Key runs by hand (no comment needed) and writes
  nothing back to the ticket — the result, the email and the PDF still come;
* "Reply on the ticket" (auto / yes / no) decides it anywhere;
* in PDF mode, work that already exists is named in the PDF, not a stop.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agent import agent as agent_module
from src.agent.agent import JiraTaskCreation
from src.document import breakdown_pdf as pdf
from src.models.schemas import ResultStatus
from tests.test_agent_webhook import DEFAULTS, Recorder, breakdown, jira_context, source_issue
from tests.test_local_pdf import _doc

DUPLICATE = {
    "proposed_title": "Receive email notifications",
    "existing_key": "KS-6635",
    "existing_summary": "Print multiple reports",
    "existing_status": "Backlog",
    "score": 0.375,
}


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch):
    def install(**results: Any) -> Recorder:
        recorder = Recorder(results)
        monkeypatch.setattr(agent_module.toolExecutor, "execute", recorder.execute)
        return recorder

    return install


def results(*, local: bool, local_pdf: bool = False, duplicates: list | None = None) -> dict:
    return {
        "run_environment": {"local_run": local},
        "read_jira_issue": source_issue(),
        "inspect_jira_context": jira_context(),
        "validate_requirement": {"status": ResultStatus.READY_FOR_JIRA.value, "requirement": "x"},
        "generate_work_breakdown": {
            "breakdown": breakdown(),
            "counts": {},
            "duplicate_matches": duplicates or [],
            "overlap_is_partial": bool(duplicates),
            "overlap": {"remaining_titles": ["Receive SMS notifications"]},
        },
        "delivery_mode": {"local_pdf": local_pdf},
        "generate_breakdown_pdf": {
            "status": "PLANNED",
            "headline": "Work breakdown emailed as a PDF — no Jira changes made",
            "summary": "The PDF was emailed.",
            "proposed": ["S1  Receive email notifications"],
        },
        "create_jira_issues": {
            "status": ResultStatus.JIRA_CREATED.value,
            "created_keys": ["KS-30"],
            "summary": "Created.",
        },
        **DEFAULTS,
    }


# --- a laptop run -------------------------------------------------------------------


async def test_a_laptop_run_needs_no_comment_and_writes_nothing_back(tools) -> None:
    recorder = tools(**results(local=True))

    result = await JiraTaskCreation.fn({"issue_key": "KS-12"})

    _, args, _ = recorder.call("read_jira_issue")
    assert args[1] is False and args[2] is False, "run by hand: no '@Aetherion' comment needed"
    assert result["status"] == ResultStatus.JIRA_CREATED.value, "the work is still done"
    assert not recorder.ran("report_to_issue"), "nothing is written back to the ticket"


async def test_a_laptop_pdf_run_still_emails_the_pdf_without_a_reply(tools) -> None:
    recorder = tools(**results(local=True))

    result = await JiraTaskCreation.fn({"issue_key": "KS-12", "output": "pdf_by_email"})

    assert recorder.ran("generate_breakdown_pdf")
    assert not recorder.ran("create_jira_issues")
    assert not recorder.ran("report_to_issue")
    assert result["status"] == ResultStatus.PLANNED.value


async def test_on_a_laptop_yes_still_replies(tools) -> None:
    recorder = tools(**results(local=True))
    await JiraTaskCreation.fn({"issue_key": "KS-12", "reply_on_ticket": "yes"})
    assert recorder.ran("report_to_issue")


async def test_a_laptop_run_answering_a_named_comment_is_not_forced(tools) -> None:
    # The local router passes the comment it is answering: that is a webhook run.
    recorder = tools(**results(local=True))
    await JiraTaskCreation.fn({"issue_key": "KS-12", "comment_id": "9001"})
    _, args, _ = recorder.call("read_jira_issue")
    assert args[1] is True, "a named comment is answered as on Aetherion"


# --- on Aetherion ---------------------------------------------------------------------


async def test_on_aetherion_the_reply_is_posted_as_before(tools) -> None:
    recorder = tools(**results(local=False))
    await JiraTaskCreation.fn({"issue_key": "KS-12"})
    _, args, _ = recorder.call("read_jira_issue")
    assert args[1] is True, "still needs the '@Aetherion' comment on Aetherion"
    assert recorder.ran("report_to_issue")


async def test_on_aetherion_no_means_nothing_written_back(tools) -> None:
    recorder = tools(**results(local=False))
    result = await JiraTaskCreation.fn({"issue_key": "KS-12", "reply_on_ticket": "no"})
    assert result["status"] == ResultStatus.JIRA_CREATED.value
    assert not recorder.ran("report_to_issue")


async def test_a_delegated_run_never_asks_where_it_is(tools) -> None:
    recorder = tools(**results(local=True))
    await JiraTaskCreation.fn({"issue_key": "KS-12", "mode": "delegated", "comment_id": "9001"})
    assert not recorder.ran("run_environment"), "the router owns the reply; nothing to decide"


async def test_a_manual_run_never_asks_where_it_is(tools) -> None:
    recorder = tools(**results(local=True))
    await JiraTaskCreation.fn(
        {"requirement": "Send email and SMS notifications.", "project_key": "KS"}
    )
    assert not recorder.ran("run_environment")


# --- PDF mode and work that already exists --------------------------------------------


async def test_pdf_mode_emails_the_pdf_even_when_part_already_exists(tools) -> None:
    recorder = tools(**results(local=False, duplicates=[DUPLICATE]))

    result = await JiraTaskCreation.fn({"issue_key": "KS-12", "output": "pdf_by_email"})

    assert result["status"] == ResultStatus.PLANNED.value, "the PDF is not withheld"
    _, args, _ = recorder.call("generate_breakdown_pdf")
    assert args[0]["report"]["already_exist"] == [DUPLICATE]
    assert not recorder.ran("create_jira_issues")


async def test_outside_pdf_mode_existing_work_still_stops_the_run(tools) -> None:
    recorder = tools(**results(local=False, duplicates=[DUPLICATE]))
    result = await JiraTaskCreation.fn({"issue_key": "KS-12", "output": "create_in_jira"})
    assert result["status"] == ResultStatus.CLARIFICATION_REQUIRED.value
    assert not recorder.ran("create_jira_issues"), "duplicates are never created"
    assert not recorder.ran("generate_breakdown_pdf")


def test_the_pdf_names_what_already_exists() -> None:
    html = pdf.breakdown_html(_doc(already_exist=[DUPLICATE]))
    assert "Already in Jira" in html
    assert "already exists as KS-6635" in html
    assert "Print multiple reports" in html
    assert "not to be created again" in html


def test_the_form_offers_the_reply_choice() -> None:
    import json
    from pathlib import Path

    meta = json.loads(Path("src/agent/metadata.json").read_text())
    field = next(t for t in meta["config"]["triggers"] if t["name"] == "reply_on_ticket")
    assert field["options"] == ["auto", "yes", "no"] and field["default"] == "auto"
