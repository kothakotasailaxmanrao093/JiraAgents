"""GENERATE_LOCAL_PDF: the breakdown as an attached PDF, and nothing else (2026-09-29).

With the flag on, "@Aetherion build" makes the whole breakdown into a PDF,
attaches it to the ticket and says what it proposes in the reply. No ticket is
created, converted, labelled or edited. With it off, the build is unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agent import agent as agent_module
from src.agent.agent import JiraTaskCreation
from src.document import breakdown_pdf as pdf
from src.models.schemas import ResultStatus, WorkBreakdown
from src.tools import tools
from tests.test_agent_webhook import DEFAULTS, Recorder, jira_context, source_issue
from tests.test_root_ticket import ORIGINAL, TYPES, large, root_spec, small


def request(breakdown: dict, **over: Any) -> dict:
    return {
        "breakdown": breakdown,
        "requirement": "Monthly invoicing for every client.",
        "request": "- Due date is 30 days after the invoice date.",
        "issue_key": "BGV-25",
        "issue_summary": "Monthly client invoicing",
        "project_key": "BGV",
        "root_key": "BGV-25",
        "agent_version": "4.3.42",
        "review_questions": ["Who is told the result?"],
        "report": {
            "related_keys": ["BGV-30 — Email invoices (relates to)"],
            "sources_read": ["BGV-25 description", "Attachment: Spec.docx"],
            "sources_missing": ["Attachment: Scan.tiff (unsupported)"],
        },
        **over,
    }


@pytest.fixture
def outbox(monkeypatch, gmail_env) -> list[dict[str, Any]]:
    """Every email the agent sends, instead of sending it."""
    from src.notifications import email as notifier

    sent: list[dict[str, Any]] = []

    def capture(host, port, sender, password, to, subject, body, **extra):
        sent.append({"to": to, "subject": subject, "body": body, **extra})

    monkeypatch.setattr(notifier, "_send_sync", capture)
    return sent


@pytest.fixture
def bgv(fake_jira, jira_env, outbox):
    def install(issuetype: str = "Task", **spec: Any):
        return fake_jira(issue_types=TYPES, epics={"BGV-25": root_spec(issuetype, **spec)})

    return install


def test_the_flag_is_off_unless_set(monkeypatch) -> None:
    assert tools.local_pdf_enabled() is False
    monkeypatch.setenv("GENERATE_LOCAL_PDF", "true")
    assert tools.local_pdf_enabled() is True


async def test_the_pdf_is_emailed_and_nothing_in_jira_changes(bgv, outbox) -> None:
    fake = bgv("Task")
    result = await tools.generate_breakdown_pdf(request(large()))

    assert result["status"] == ResultStatus.PLANNED.value
    assert fake.writes == [], "no create, convert, label, edit or attachment"
    assert fake.epics["BGV-25"]["issuetype"] == "Task"
    assert fake.epics["BGV-25"]["description"] == ORIGINAL
    [mail] = outbox
    assert mail["to"] == ["lead@example.com", "pm@example.com"]
    [attached] = mail["attachments"]
    assert attached.filename == result["attachment"]
    assert attached.data.startswith(b"%PDF")
    assert "No Jira tickets were created or changed" in mail["body"]
    assert "No Jira tickets were created or changed" in mail["html"]
    assert "BGV-25" in mail["subject"]
    assert result["emailed_to"] == ["lead@example.com", "pm@example.com"]
    assert "was emailed to lead@example.com, pm@example.com" in result["summary"]
    assert "BGV-25 would become an Epic (it is a Task now)" in result["summary"]


async def test_a_pdf_that_cannot_be_emailed_says_so(bgv, monkeypatch) -> None:
    from src.notifications import email as notifier

    def down(*_a, **_k):
        raise ConnectionError("smtp unreachable")

    monkeypatch.setattr(notifier, "_send_sync", down)
    bgv("Task")
    result = await tools.generate_breakdown_pdf(request(large()))
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "could not be emailed" in result["reason"]


async def test_the_reply_numbers_what_the_pdf_proposes(bgv) -> None:
    bgv("Task")
    result = await tools.generate_breakdown_pdf(request(large()))
    proposed = result["proposed"]
    assert proposed[0].startswith("S1  Generate Monthly Invoices for Completed Checks")
    assert "S1.1 Collect completed checks" in proposed[0]
    assert not any(line.startswith("E ") for line in proposed), "the root is the Epic"


async def test_without_a_root_the_epic_is_its_own_item(bgv) -> None:
    bgv("Task")
    result = await tools.generate_breakdown_pdf(request(large(), root_key=""))
    assert result["proposed"][0].startswith("E  Epic — ")


EPIC_69 = {"key": "BGV-69", "summary": "Candidate Address Verification"}


async def test_an_existing_epic_from_the_form_is_where_the_stories_go(bgv, outbox) -> None:
    """As creation: the Stories go under the named Epic and no new Epic is made."""
    bgv("Task")
    result = await tools.generate_breakdown_pdf(
        request(large(), root_key="", existing_epic=EPIC_69)
    )
    proposed = result["proposed"]
    assert proposed[0] == (
        "BGV-69 — Candidate Address Verification "
        "(existing Epic — no new Epic; the Stories go under it)"
    )
    assert not any(line.startswith("E ") for line in proposed), "no new Epic"
    assert proposed[1].startswith("S1  ")
    assert "BGV-69" in outbox[0]["body"]


async def test_a_root_ignores_the_existing_epic_as_creation_does(bgv) -> None:
    bgv("Task")
    result = await tools.generate_breakdown_pdf(request(large(), existing_epic=EPIC_69))
    assert not any("BGV-69" in line for line in result["proposed"])


def test_the_pdf_puts_the_stories_under_the_existing_epic() -> None:
    html = pdf.breakdown_html(
        _doc(root=None, existing_epic="BGV-69 — Candidate Address Verification")
    )
    assert "Existing Epic: BGV-69 — Candidate Address Verification" in html
    assert "No new Epic would be made" in html
    assert "new Stories under the existing Epic BGV-69" in html  # at a glance
    assert "E — Epic" not in html and "E  Epic" not in html


async def test_the_same_breakdown_twice_is_emailed_once(bgv, outbox) -> None:
    bgv("Task")
    first = await tools.generate_breakdown_pdf(request(large()))
    second = await tools.generate_breakdown_pdf(request(large()))
    assert len(outbox) == 1, "a redelivered request does not email again"
    assert first["status"] == second["status"] == ResultStatus.PLANNED.value


async def test_a_root_that_could_not_be_built_is_said_before_any_pdf(bgv, outbox) -> None:
    fake = bgv("Subtask", subtask=True)
    result = await tools.generate_breakdown_pdf(request(large()))
    assert result["status"] == ResultStatus.JIRA_CREATION_FAILED.value
    assert "is a Sub-task" in result["reason"]
    assert fake.writes == [] and outbox == []


# --- the document ---------------------------------------------------------------------


def _doc(**over: Any) -> pdf.BreakdownDocument:
    fields = dict(
        issue_key="BGV-25",
        issue_summary="Monthly client invoicing",
        project_key="BGV",
        generated_at="2026-09-29 10:30 UTC",
        agent_version="4.3.42",
        breakdown=WorkBreakdown.model_validate(large()),
        root=pdf.RootPreview("BGV-25", "Task", "Epic", "Monthly client invoicing", "Automate"),
        related_keys=["BGV-30 — Email invoices (relates to)"],
        sources_read=["BGV-25 description"],
        sources_missing=["Attachment: Scan.tiff (unsupported)"],
        requirement_lines=["Due date is 30 days after the invoice date."],
        review_questions=["Who is told the result?"],
    )
    fields.update(over)
    return pdf.BreakdownDocument(**fields)


def test_the_pdf_holds_everything_clearly() -> None:
    html = pdf.breakdown_html(_doc())
    for expected in (
        pdf.MODE_LINE,
        "Tickets (card numbers)",
        "BGV-25 — Monthly client invoicing (asked on)",
        "BGV-30 — Email invoices",
        "Type: Task → Epic",
        "Sources read",
        "Scan.tiff (unsupported)",
        "Confirmed facts",
        "30 days",
        "From the review: Who is told the result?",
        "Proposed tickets (not created)",
        "S1 — Generate Monthly Invoices for Completed Checks",
        "S1.1 Collect completed checks",
        "Acceptance criteria",
        "Done when:",
        "Which ticket delivers each requirement line",
    ):
        assert expected in html, expected


def test_the_pdf_escapes_what_people_wrote() -> None:
    assert "<script>" not in pdf.breakdown_html(_doc(issue_summary="<script>x</script>"))


def test_it_renders_to_a_real_pdf_with_page_numbers() -> None:
    import fitz

    data = pdf.render_pdf(_doc())
    assert data.startswith(b"%PDF")
    pages = fitz.open(stream=data, filetype="pdf")
    last = pages[pages.page_count - 1].get_text()
    assert f"Page {pages.page_count} of {pages.page_count} · BGV-25 work breakdown" in last
    assert "At a glance" in pages[0].get_text()


# --- the workflow ---------------------------------------------------------------------


@pytest.fixture
def run(monkeypatch):
    def install(**results: Any) -> Recorder:
        recorder = Recorder(results)
        monkeypatch.setattr(agent_module.toolExecutor, "execute", recorder.execute)
        return recorder

    return install


def _results(local_pdf: bool) -> dict:
    return {
        "read_jira_issue": source_issue(ticket_is_root=True, trigger_is_pointer=True),
        "inspect_jira_context": jira_context(),
        "validate_requirement": {
            "status": "READY_FOR_JIRA",
            "requirement": "x",
            "request_text": "x",
        },
        "generate_work_breakdown": {"breakdown": small(), "counts": {}, "duplicate_matches": []},
        "delivery_mode": {"local_pdf": local_pdf},
        "generate_breakdown_pdf": {
            "status": "PLANNED",
            "headline": "Work breakdown ready as a PDF — no Jira changes made",
            "summary": f"{pdf.MODE_LINE}. The PDF was emailed to lead@example.com.",
            "proposed": ["S1  Verify Candidate ID Documents — Sub-tasks: S1.1 Step 1"],
            "attachment": "aetherion-breakdown-KS-12-abcd1234.pdf",
        },
        "create_jira_issues": {"status": "JIRA_CREATED", "created_keys": ["KS-30"]},
        **DEFAULTS,
    }


async def test_with_the_flag_on_a_build_ends_in_the_pdf(run) -> None:
    recorder = run(**_results(local_pdf=True))
    result = await JiraTaskCreation.fn(
        {"issue_key": "KS-12", "mode": "delegated", "review_questions": ["Who approves?"]}
    )
    assert result["outcome"] == "PLANNED"
    assert result["headline"] == "Work breakdown ready as a PDF — no Jira changes made"
    assert result["proposed"] == ["S1  Verify Candidate ID Documents — Sub-tasks: S1.1 Step 1"]
    assert "From the review: Who approves?" in result["questions"]
    assert not recorder.ran("create_jira_issues")
    _, args, _ = recorder.call("generate_breakdown_pdf")
    assert args[0]["root_key"] == "KS-12"


async def test_the_epic_named_on_the_form_reaches_the_pdf(run) -> None:
    results = _results(local_pdf=True)
    results["inspect_jira_context"] = {**jira_context(), "epic": {"key": "KS-5", "summary": "X"}}
    recorder = run(**results)
    await JiraTaskCreation.fn({"issue_key": "KS-12", "mode": "delegated"})
    _, args, _ = recorder.call("generate_breakdown_pdf")
    assert args[0]["existing_epic"] == {"key": "KS-5", "summary": "X"}


async def test_with_the_flag_off_the_build_is_unchanged(run) -> None:
    recorder = run(**_results(local_pdf=False))
    result = await JiraTaskCreation.fn(
        {"issue_key": "KS-12", "mode": "delegated", "review_questions": ["Who approves?"]}
    )
    assert result["outcome"] == "CREATED"
    assert recorder.ran("create_jira_issues") and not recorder.ran("generate_breakdown_pdf")
    assert "From the review: Who approves?" in result["questions"]


async def test_a_manual_run_with_no_ticket_still_emails_the_pdf(
    fake_jira, jira_env, outbox
) -> None:
    """Run from the agent's form with a typed-in requirement: no ticket to name it by."""
    fake_jira(issue_types=TYPES)
    result = await tools.generate_breakdown_pdf(request(large(), issue_key="", root_key=""))
    assert result["status"] == ResultStatus.PLANNED.value
    assert result["attachment"].startswith("aetherion-breakdown-BGV-REQUEST-")
    assert len(outbox) == 1


# --- the form's "Output" choice ----------------------------------------------------------


@pytest.mark.parametrize(
    "choice,setting,expect_pdf,asks_setting",
    [
        ("pdf_by_email", False, True, False),
        ("create_in_jira", True, False, False),
        ("use_setting", True, True, True),
        ("use_setting", False, False, True),
        ("", True, True, True),  # a Jira comment through the router sets nothing
    ],
)
async def test_the_form_can_choose_pdf_or_tickets(run, choice, setting, expect_pdf, asks_setting):
    recorder = run(**_results(local_pdf=setting))
    await JiraTaskCreation.fn({"issue_key": "KS-12", "mode": "delegated", "output": choice})
    assert recorder.ran("generate_breakdown_pdf") is expect_pdf
    assert recorder.ran("create_jira_issues") is not expect_pdf
    assert recorder.ran("delivery_mode") is asks_setting


@pytest.mark.parametrize(
    "choice,setting,expect_pdf",
    [
        ("pdf_by_email", False, True),
        ("use_setting", True, True),
        ("use_setting", False, False),
        ("create_in_jira", True, False),
    ],
)
async def test_pdf_mode_emails_even_with_create_issues_off(run, choice, setting, expect_pdf):
    """PDF mode never creates anything, so "Create Issues In Jira = No" must not
    stop the email (2026-09-30: the form run ended in a silent preview)."""
    recorder = run(**_results(local_pdf=setting))
    result = await JiraTaskCreation.fn(
        {"issue_key": "KS-12", "mode": "delegated", "output": choice, "create_in_jira": False}
    )
    assert recorder.ran("generate_breakdown_pdf") is expect_pdf
    assert not recorder.ran("create_jira_issues"), "No means nothing is created"
    assert (result["outcome"] == "PLANNED") is expect_pdf


def test_the_form_offers_the_choice() -> None:
    import json
    from pathlib import Path

    triggers = json.loads(Path("src/agent/metadata.json").read_text())["config"]["triggers"]
    output = next(t for t in triggers if t["name"] == "output")
    assert output["options"] == ["use_setting", "pdf_by_email", "create_in_jira"]
    assert output["default"] == "use_setting"


def test_every_dropdown_lists_its_choices_in_its_description() -> None:
    """Aetherion's form builds a dropdown from its description split at commas,
    not from "options" — prose there became nonsense choices (2026-09-30)."""
    import json
    from pathlib import Path

    triggers = json.loads(Path("src/agent/metadata.json").read_text())["config"]["triggers"]
    for t in triggers:
        if t.get("type") == "dropdown":
            listed = [c.strip() for c in t["description"].split(",")]
            assert listed == t["options"], t["name"]
