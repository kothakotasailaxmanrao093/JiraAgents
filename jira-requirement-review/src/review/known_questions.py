"""Open questions a ticket already lists, and findings that only repeat them.

Work Breakdown writes "Open questions — not stated in the requirement" on every
Story it creates. Told in the system prompt to skip them, the reviewer still
raised all three on BGV-108 (2026-09-25), so the team saw known gaps again and
had to hunt for the new ones. The questions are now handed to the model by name,
and any finding that plainly restates one is dropped here as well.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Written by jira-task-creation src/jira/adf.py::story_description.
HEADING = "Open questions — not stated in the requirement"

_STOP = frozenset(
    """what which when where does should would could must there their about the this that
    with from into have been will whether handled happens specify specified defined
    requirement behavior behaviour system""".split()
)


def listed_open_questions(documents: list[dict[str, Any]]) -> list[str]:
    """The open questions in the target's description AND its parent's.

    A Sub-task's gaps are asked on its Story: BGV-32's review raised the email
    content "even though the parent ticket flags this as an open question"
    (2026-09-25).
    """
    out: list[str] = []
    for doc in documents:
        label = str(doc.get("source_label") or "")
        if not (label.startswith(("Target ", "Parent ")) and label.endswith("(description)")):
            continue
        lines = [line.strip() for line in str(doc.get("text") or "").splitlines()]
        if HEADING not in lines:
            continue
        for line in lines[lines.index(HEADING) + 1 :]:
            if not line.startswith("- "):
                break
            out.append(line[2:].strip())
    return list(dict.fromkeys(out))


def _stems(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return {re.sub(r"(ing|ed|es|s)$", "", w) for w in words if len(w) > 3 and w not in _STOP}


def repeats(finding: str, question: str) -> bool:
    """Whether a finding says no more than a question already listed.

    Most of the question's meaningful words must appear in the finding.
    BGV-108: "How is email delivery failure handled?" against "There is no
    defined behavior for handling email delivery failures (e.g. retries…)".
    """
    q = _stems(question)
    return bool(q) and len(q & _stems(finding)) / len(q) >= 0.6


def drop_repeats(findings: list[dict[str, Any]], questions: list[str]) -> list[dict[str, Any]]:
    return [
        f
        for f in findings
        if not any(repeats(str(f.get("description") or ""), q) for q in questions)
    ]


def drop_ungrounded(
    findings: list[dict[str, Any]], documents: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Keep only findings whose evidence cites a source that was actually read.

    Review is a model and can over-reach — "the notification API contract is
    not specified" on a ticket that never mentioned an API. The prompt already
    demands a verbatim source label per finding; this enforces it. The count
    dropped is the review's over-reach metric, reported, never charged to the
    ticket (D1 bar 2, 2026-09-25).
    """
    labels = [_label(d.get("source_label")) for d in documents if d.get("source_label")]

    def cited(finding: dict[str, Any]) -> bool:
        evidence = _label(finding.get("evidence_source"))
        return bool(evidence) and any(lab == evidence or lab in evidence for lab in labels)

    kept = [f for f in findings if cited(f)]
    if findings and not kept:
        # Every finding "ungrounded" is a label format mismatch, not a review
        # that invented everything — keep them rather than blank the review.
        logger.warning("no finding cited a known source label; keeping all (format mismatch?)")
        return findings, 0
    return kept, len(findings) - len(kept)


def _label(value: Any) -> str:
    """A source label, compared loosely: case, quotes, "[SOURCE: …]", spacing."""
    text = str(value or "").lower()
    text = re.sub(r"^\s*\[?\s*source\s*:\s*", "", text)
    text = re.sub(r"[\"'\]\[]", "", text)
    return re.sub(r"\s+", " ", text).strip()


# Technical things a finding can demand that the input may never have raised.
# BGV-18 said only "Verify the address of the candidate quickly." and its review
# asked for "API contracts, authentication" and "data model requirements"
# (2026-09-25) — the brief's own over-reach example, and it cited a real source
# label, so the label check alone could not catch it.
# Matched as word STARTS, so "queue" also catches "queued" and "queues" — BGV-32's
# "queued/asynchronous" slipped past an exact match (2026-09-25).
_TECH_TERMS = (
    "api",
    "endpoint",
    "database",
    "data model",
    "schema",
    "microservice",
    "service",
    "integration",
    "queue",
    "webhook",
    "cache",
    "sdk",
    "library",
    "framework",
    "authentication",
    "encryption",
    "infrastructure",
    "server",
    "logging",
    "monitoring",
    "batch job",
    "cron",
    "smtp",
    "synchronous",
    "asynchronous",
    "async",
    "encoding",
    "timeout",
    "rate limit",
    "idempoten",
)


def drop_invented_topics(
    findings: list[dict[str, Any]], documents: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Drop findings that demand a technical thing no source mentions.

    A finding counts only if it points at something the input raised, or at a
    rule stated in a way that cannot be tested (D1 bar 2). "No API contract is
    given" on a ticket that never mentions an API is neither: it is over-reach,
    counted with the rest, never charged to the ticket.
    """
    source = " ".join(str(d.get("text") or "") for d in documents).lower()

    def invents(finding: dict[str, Any]) -> bool:
        text = str(finding.get("description") or "").lower()
        for term in _TECH_TERMS:
            if re.search(rf"\b{re.escape(term)}", text) and not re.search(
                rf"\b{re.escape(term)}", source
            ):
                return True
        return False

    kept = [f for f in findings if not invents(f)]
    return kept, len(findings) - len(kept)
