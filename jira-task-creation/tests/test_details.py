"""Hard details: stated facts the mapped ticket must carry (BGV-41, 2026-09-25).

BGV-41 mapped all 22 lines and still dropped "Price missing", "every billing
contact", "30 days after the invoice date" and "only one reminder". Lines below
are BGV-41's own wording; each edge case is a way extraction or matching went
wrong on real text before it was right.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.classification import decompose
from src.classification.decompose import llm_breakdown
from src.classification.details import Detail, carried, hard_details, missing_details
from src.classification.validate import ProjectContext
from src.models.schemas import WorkBreakdown


def texts(line: str) -> list[tuple[str, str]]:
    return [(d.kind, d.text) for d in hard_details(line)]


# --- extraction ---------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            'If a check type has no price in the contract, the line is marked "Price missing".',
            [("quoted", "Price missing")],
        ),
        (
            "On the 1st of every month at 06:00 IST, the system creates one invoice per client.",
            [("clock", "06:00")],
        ),
        ("Due date is 30 days after the invoice date.", [("number", "30 days")]),
        ("Only one reminder is sent per invoice.", [("limit", "only one reminder")]),
        (
            "The invoice is emailed to every billing contact on the client's record.",
            [("every", "billing contact")],
        ),
        (
            "Each invoice gets a number in the format INV-YYYYMM-<client code>, e.g. INV-202609-ACME.",
            [("identifier", "INV-YYYYMM")],
        ),
        ("Upload a certificate as PDF or JPG, max 8 MB.", [("number", "8 mb")]),
        (
            "Flag an employer who does not respond in 5 working days.",
            [("number", "5 working days")],
        ),
    ],
)
def test_the_stated_facts_are_extracted(line: str, expected: list) -> None:
    assert texts(line) == expected


def test_an_apostrophe_is_not_a_quote() -> None:
    line = "It appears in the billing officer's \"Not sent\" list with the client's reason."
    assert texts(line) == [("quoted", "Not sent")]


def test_a_quoted_template_with_placeholders_is_not_demanded_word_for_word() -> None:
    assert texts('Email subject: "Invoice <invoice number> for <month year>".') == []


def test_every_stops_at_a_linking_word() -> None:
    line = "Client users can see all invoices from the last 24 months, newest first."
    assert ("every", "invoices") in texts(line)


def test_every_month_is_timing_not_a_fact_to_carry() -> None:
    assert all(kind != "every" for kind, _ in texts("On the 1st of every month, invoices run."))


def test_a_line_with_no_hard_facts_yields_nothing() -> None:
    assert texts("A billing officer can resend any invoice manually.") == []


# --- matching -----------------------------------------------------------------


@pytest.mark.parametrize(
    "detail,text,ok",
    [
        (Detail("number", "30 days"), "due 30 days after the invoice date", True),
        (Detail("number", "30 days"), "due thirty days after", False),
        (Detail("number", "5 working days"), "within 5 days", False),  # "working" matters
        (Detail("number", "24 months"), "invoices from the last 24 months", True),
        (Detail("limit", "only one reminder"), "a single reminder is sent", True),
        (Detail("limit", "only one reminder"), "reminders are sent", False),
        (Detail("every", "billing contact"), "emailed to all billing contacts", True),
        (Detail("every", "billing contact"), "emailed to the billing contact", False),
        (Detail("quoted", "Price missing"), 'the line is marked "price missing"', True),
        (Detail("identifier", "INV-YYYYMM"), "number format inv-yyyymm-<client code>", True),
    ],
)
def test_carried_matches_the_fact_not_just_similar_words(detail, text, ok) -> None:
    assert carried(detail, text) is ok


# --- against a breakdown ------------------------------------------------------------


def _story(title: str, criteria: list[str], subtask_titles: list[str]) -> dict[str, Any]:
    return {
        "title": title,
        "user_story_statement": f"As a billing officer, I want to {title.lower()}, so that clients pay.",
        "description": f"{title}.",
        "business_value": "Clients are billed.",
        "priority": "High",
        "estimated_complexity": "Medium",
        "dependencies": "None",
        "acceptance_criteria": criteria,
        "subtasks": [
            {
                "title": t,
                "description": f"{t} for each client invoice.",
                "expected_outcome": f"{t} is done.",
                "dependencies": "None",
                "completion_criteria": f"A test client shows {t.lower()} on its June invoice.",
            }
            for t in subtask_titles
        ],
    }


LINES = [
    "Due date is 30 days after the invoice date.",
    "30 days after the due date, if still unpaid, one reminder email goes to the billing contacts.",
]


def _two_stories(send_criteria: list[str]) -> WorkBreakdown:
    return WorkBreakdown.model_validate(
        {
            "classification": "Medium",
            "analysis": "Sending and overdue handling.",
            "epic": {
                "business_objective": "Bill clients monthly.",
                "scope": ["Invoices"],
                "out_of_scope": ["Not specified"],
                "priority": "High",
                "acceptance_criteria": ["Invoices go out monthly."],
                "jira_summary": "Monthly invoicing",
            },
            "stories": [
                _story("Send Invoices via Email", send_criteria, ["Email PDF", "Set due date"]),
                _story(
                    "Handle Overdue Invoices",
                    ["Given an invoice overdue by 30 days, then a reminder is sent"],
                    ["Find overdue", "Send reminder"],
                ),
            ],
        }
    )


COVERAGE = {"1": "Send Invoices via Email", "2": "Handle Overdue Invoices"}


def test_a_number_in_another_story_does_not_vouch_for_this_line() -> None:
    """BGV-41: "30 days" in the overdue Story hid the missing due date."""
    breakdown = _two_stories(["Given an invoice, when sent, then it reaches the contact"])
    gaps = missing_details(LINES, COVERAGE, breakdown)
    assert [(n, d.text, where) for n, d, where in gaps] == [
        (1, "30 days", "Send Invoices via Email")
    ]


def test_the_fact_in_the_mapped_story_passes() -> None:
    breakdown = _two_stories(["Given an invoice dated 1 June, then its due date is 30 days later"])
    assert missing_details(LINES, COVERAGE, breakdown) == []


async def test_a_missing_fact_is_named_in_the_one_regeneration(monkeypatch) -> None:
    lines_text = "- " + "\n- ".join(LINES)
    thin = _two_stories(["Given an invoice, when sent, then it reaches the contact"])
    fixed = _two_stories(["Given an invoice dated 1 June, then its due date is 30 days later"])
    prompts: list[str] = []

    async def chat(prompt: str) -> str:
        prompts.append(prompt)
        if prompt.startswith("Check this proposed Jira breakdown"):
            return json.dumps({})
        if "Rewrite ONLY the completion criteria" in prompt:
            return json.dumps({"criteria": {}})
        model = fixed if "Your previous answer was rejected" in prompt else thin
        return json.dumps({**model.model_dump(mode="json"), "coverage": COVERAGE})

    monkeypatch.setattr(decompose, "_chat", chat)
    breakdown = await llm_breakdown(lines_text, ProjectContext())
    retry = next(p for p in prompts if "Your previous answer was rejected" in p)
    assert 'line 1 states "30 days" — not in Send Invoices via Email' in retry
    assert breakdown._quality_notes == []


async def test_a_fact_the_model_keeps_dropping_is_carried_in_the_requirements_words(
    monkeypatch,
) -> None:
    """BGV-41 (2026-09-25): named in the retry, still dropped. Now it cannot be:
    the focused request is tried, then the line itself becomes a criterion."""
    lines_text = "- " + "\n- ".join(LINES)
    thin = _two_stories(["Given an invoice, when sent, then it reaches the contact"])

    async def chat(prompt: str) -> str:
        if prompt.startswith("Check this proposed Jira breakdown"):
            return json.dumps({})
        if "Rewrite ONLY the completion criteria" in prompt:
            return json.dumps({"criteria": {}})
        if prompt.startswith("Some stated facts of this requirement are missing"):
            return json.dumps({"criteria": {"Send Invoices via Email": ["Given an invoice…"]}})
        return json.dumps({**thin.model_dump(mode="json"), "coverage": COVERAGE})

    monkeypatch.setattr(decompose, "_chat", chat)
    breakdown = await llm_breakdown(lines_text, ProjectContext())
    send = breakdown.stories[0]
    assert send.acceptance_criteria[-1] == (
        'As stated in the requirement: "Due date is 30 days after the invoice date."'
    )
    assert breakdown._quality_notes == []


async def test_a_focused_request_that_carries_the_fact_is_used(monkeypatch) -> None:
    lines_text = "- " + "\n- ".join(LINES)
    thin = _two_stories(["Given an invoice, when sent, then it reaches the contact"])
    good = "Given an invoice dated 1 June, when it is sent, then its due date is 30 days later"

    async def chat(prompt: str) -> str:
        if prompt.startswith("Check this proposed Jira breakdown"):
            return json.dumps({})
        if "Rewrite ONLY the completion criteria" in prompt:
            return json.dumps({"criteria": {}})
        if prompt.startswith("Some stated facts of this requirement are missing"):
            assert '"30 days" (line 1' in prompt
            return json.dumps({"criteria": {"Send Invoices via Email": [good]}})
        return json.dumps({**thin.model_dump(mode="json"), "coverage": COVERAGE})

    monkeypatch.setattr(decompose, "_chat", chat)
    breakdown = await llm_breakdown(lines_text, ProjectContext())
    assert breakdown.stories[0].acceptance_criteria[-1] == good


def test_the_note_says_every_billing_contact_not_billing_contact() -> None:
    from src.classification.details import phrase

    assert phrase(Detail("every", "billing contact")) == "every billing contact"


# --- BGV-32: one sentence said three times ---------------------------------------


def test_a_completion_criterion_that_restates_the_description_is_vague() -> None:
    from src.classification.decompose import _repeats

    assert _repeats(
        "Email with PDF is sent to the client's billing contact.",
        "Email the generated PDF to the client's billing contact.",
    )


def test_a_concrete_example_check_is_not_a_restatement() -> None:
    from src.classification.decompose import _repeats

    assert not _repeats(
        "For a client with 3 checks completed in June, on 1 July every billing contact "
        "receives one email with the June invoice PDF.",
        "Email the generated PDF to the client's billing contact.",
    )


# --- BGV-41 rebuild: the two rules with no number or quote ------------------------


def test_an_idempotency_rule_is_a_hard_detail() -> None:
    line = "If the monthly run is started again, invoices that already exist are not created twice."
    assert ("not_twice", "not twice") in texts(line)
    assert carried(Detail("not_twice", "not twice"), "then no duplicate invoices are created")
    assert not carried(Detail("not_twice", "not twice"), "then an invoice is created per client")


def test_an_audit_rule_keeps_its_fields() -> None:
    line = "A billing officer can resend any invoice; each resend is recorded with date, time and who resent it."
    assert ("recorded", "date, time, who") in texts(line)
    detail = Detail("recorded", "date, time, who")
    assert carried(detail, "each resend is logged with its date, time and who resent it")
    assert not carried(detail, "the billing officer can resend the invoice")
    assert not carried(detail, "each resend is logged with its date")  # a field dropped
