"""D1 bar 2: every review finding must cite a source that was actually read.

Test C — an ungrounded finding is dropped and counted as over-reach.
Test D — review of a ticket a person wrote, which the agent never touched,
still reports its real gaps: grounding must not blank a human ticket's review.
"""

from __future__ import annotations

import json
from typing import Any

from review.known_questions import drop_ungrounded
from tools import analyze_requirement_tool as tool

BGV41 = "Target BGV-41 (description)"
DOCS = [
    {
        "source_label": BGV41,
        "kind": "requirement",
        "text": (
            "As a recruiter, I want to raise a verification request for each of a "
            "candidate's last two employers.\nAcceptance criteria\n- If an employer does "
            "not respond within 5 working days, the request is flagged to the recruiter."
        ),
    }
]


def test_c_a_finding_citing_nothing_read_is_dropped_and_counted() -> None:
    findings = [
        {"description": "Working days are not defined.", "evidence_source": BGV41},
        # The brief's over-reach example: the ticket never mentions an API.
        {
            "description": "The notification API contract is not specified.",
            "evidence_source": "Architecture guidelines",
        },
    ]
    kept, over_reach = drop_ungrounded(findings, DOCS)
    assert [f["description"] for f in kept] == ["Working days are not defined."]
    assert over_reach == 1


def test_labels_are_matched_loosely() -> None:
    kept, over_reach = drop_ungrounded(
        [{"description": "x", "evidence_source": "[SOURCE: target bgv-41 (Description)]"}], DOCS
    )
    assert len(kept) == 1 and over_reach == 0


def test_a_label_format_mismatch_never_blanks_the_review() -> None:
    findings = [{"description": "x", "evidence_source": "description"}]
    kept, over_reach = drop_ungrounded(findings, DOCS)
    assert kept == findings and over_reach == 0


class _Gateway:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer

    async def chat(self, **_: Any) -> dict[str, str]:
        return {"content": json.dumps(self.answer)}


async def test_d_a_human_written_ticket_is_reviewed_normally(monkeypatch) -> None:
    human = [
        {
            "source_label": "Target BGV-12 (description)",
            "kind": "requirement",
            "text": "Verify the address of the candidate quickly.",
        }
    ]
    answer = {
        "findings": [
            {
                "finding_type": "Ambiguity",
                "description": "'quickly' has no measurable target.",
                "evidence_source": "Target BGV-12 (description)",
                "confidence": "high",
                "priority": "high",
            },
            {
                "finding_type": "Missing Acceptance Criterion",
                "description": "There are no acceptance criteria.",
                "evidence_source": "Target BGV-12 (description)",
                "confidence": "high",
            },
        ],
        "overall_readiness": "not_ready",
        "readiness_score": 1,
        "executive_summary": "Too vague to start.",
    }
    import agent_lib.gateway.ai as gateway

    monkeypatch.setattr(gateway, "ai_gateway", _Gateway(answer))
    fn = getattr(tool.analyze_requirement, "__wrapped__", tool.analyze_requirement)
    out = await fn(human, "BGV-12", "Address check")
    assert len(out["findings"]) == 2
    assert out["over_reach"] == 0
    assert out["known_questions"] == []


# --- BGV-18: over-reach that cited a real label ---------------------------------

BGV18 = [
    {
        "source_label": "Target BGV-18 (description)",
        "kind": "requirement",
        "text": "Verify the address of the candidate quickly.",
    }
]
BGV18_FINDINGS = [
    {"description": "The term 'quickly' is ambiguous: no performance target is defined."},
    {
        "description": "No details are provided about any external or internal address "
        "verification service to be used, including API contracts, authentication, or "
        "error handling."
    },
    {
        "description": "Data model requirements for storing verified addresses and their "
        "verification status are not described."
    },
    {"description": "The expected behavior when an address cannot be verified is not defined."},
]


def test_findings_demanding_technology_the_ticket_never_mentions_are_dropped() -> None:
    from review.known_questions import drop_invented_topics

    kept, over_reach = drop_invented_topics(BGV18_FINDINGS, BGV18)
    assert over_reach == 2
    assert [f["description"][:20] for f in kept] == [
        "The term 'quickly' i",
        "The expected behavio",
    ]


def test_a_technical_finding_about_something_the_ticket_does_mention_is_kept() -> None:
    from review.known_questions import drop_invented_topics

    docs = [{"text": "Send the report through the payments API when the case closes."}]
    finding = [{"description": "The API's error handling for a failed send is not specified."}]
    kept, over_reach = drop_invented_topics(finding, docs)
    assert kept == finding and over_reach == 0


# --- BGV-32: a Sub-task reviewed on its own --------------------------------------

BGV32 = [
    {
        "source_label": "Target BGV-32 (description)",
        "kind": "requirement",
        "text": "Description\nEmail the generated PDF to the client's billing contact.\n"
        "Completion criteria\nEmail with PDF is sent to the client's billing contact.",
    },
    {
        "source_label": "Parent BGV-30 (description)",
        "kind": "parent_context",
        "text": "Acceptance criteria\n- Given an invoice is generated, when sent, then the "
        "contact receives it.\nOpen questions — not stated in the requirement\n"
        "- What should be the subject and body content of the invoice email?",
    },
]


def test_the_parents_open_questions_count_as_already_listed() -> None:
    from review.known_questions import listed_open_questions

    assert listed_open_questions(BGV32) == [
        "What should be the subject and body content of the invoice email?"
    ]


def test_bgv32s_technical_over_reach_is_dropped_in_any_word_form() -> None:
    from review.known_questions import drop_invented_topics

    findings = [
        {"description": "Failure handling for an SMTP outage, attachment too large, or timeout."},
        {"description": "Whether the email is sent synchronously or can be queued/asynchronous."},
        {"description": "Constraints on the PDF attachment size or encoding are not given."},
        {"description": "It is not defined how the billing contact's email is obtained."},
    ]
    kept, over_reach = drop_invented_topics(findings, BGV32)
    assert over_reach == 3
    assert kept == [findings[3]]


def test_a_subtask_is_judged_by_completion_criteria_not_acceptance_criteria() -> None:
    from review.completeness import sources_missing

    target = {
        "key": "BGV-32",
        "is_subtask": True,
        "description_text": BGV32[0]["text"],
    }
    gaps = [g.what for g in sources_missing(target, BGV32, [])]
    assert not any("acceptance criteria" in g for g in gaps)


def test_the_reviewer_is_told_the_target_is_a_subtask() -> None:
    from review.prompt import build_user_message

    message = build_user_message("BGV-32", "Email Invoice PDF", BGV32, [], True)
    assert "THE TARGET IS A SUB-TASK" in message
