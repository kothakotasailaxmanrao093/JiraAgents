"""Deterministic de-duplication of findings.

The LLM is also instructed to avoid duplicates, but this is a cheap, testable
safety net: two findings of the same type whose descriptions normalise to the
same text are collapsed to the first occurrence (preserving order).
"""

from __future__ import annotations

import re

from .models import Finding, Review

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for fuzzy equality."""
    return _WS.sub(" ", _PUNCT.sub("", text.lower())).strip()


def dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str]] = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.finding_type.value, _normalize(f.description))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def dedupe_review(review: Review) -> Review:
    return Review(findings=dedupe(review.findings))
