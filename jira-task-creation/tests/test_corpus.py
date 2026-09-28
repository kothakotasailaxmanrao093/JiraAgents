"""Grammatical-shape corpus.

Every defect found in live testing was the same thing: a hand-written rule
meeting a sentence shape nobody had tried. This file is the answer — a broad
set of shapes, each asserted for classification and for output that reads like
English. Add a row here whenever a new shape surprises you.

These run on the heuristic path (no gateway), which is the weaker of the two.
"""

from __future__ import annotations

import pytest

from src.classification.decompose import extract_actor, heuristic_breakdown, split_capabilities
from src.classification.validate import ProjectContext, validate_input
from src.models.schemas import Classification

CTX = ProjectContext(name="FleetLink", description="Delivery fleet management platform.")
GENERIC_ACTOR = "user of this product"

# (label, requirement, expected classification or None if not asserted)
CORPUS: list[tuple[str, str, Classification | None]] = [
    ("imperative", "Send a daily summary email to each dispatcher.", Classification.SMALL),
    ("allow-to", "Allow drivers to upload a proof-of-delivery photo.", Classification.SMALL),
    ("let-indirect", "Let dispatchers assign a job to a driver.", Classification.SMALL),
    ("let-article", "Let a driver report a vehicle fault.", Classification.SMALL),
    ("modal-could", "Operations managers could see fuel costs per vehicle.", Classification.SMALL),
    ("modal-should", "A dispatcher should be able to cancel a job.", Classification.SMALL),
    (
        "passive",
        "Jobs must be automatically reassigned when a driver declines.",
        Classification.SMALL,
    ),
    (
        "pasted-user-story",
        "As a driver, I want to see my next stop on a map.",
        Classification.SMALL,
    ),
    ("question", "Can we add a way for drivers to flag a damaged parcel?", Classification.SMALL),
    ("need-to", "We need to track tyre replacement dates for every van.", Classification.SMALL),
    ("gerund", "Tracking fuel card usage per driver.", Classification.SMALL),
    (
        "so-that-inline",
        "Add a depot filter so that dispatchers can narrow the job list.",
        Classification.SMALL,
    ),
    (
        "multi-sentence",
        "Drivers need a mobile checklist. It should be completed before each trip.",
        None,
    ),
    (
        "bulleted",
        "Add reporting:\n- fuel spend by depot\n- idle time by vehicle\n- overtime by driver",
        Classification.MEDIUM,
    ),
    (
        "shared-verb-3",
        "Track servicing, insurance renewal, and accident reports.",
        Classification.MEDIUM,
    ),
    (
        "shared-verb-6",
        "Support onboarding, licensing, scheduling, payroll, invoicing, and offboarding.",
        Classification.LARGE,
    ),
    (
        "because-tail",
        "Add bulk close, because closing jobs one by one and clicking through is slow.",
        Classification.SMALL,
    ),
    (
        "which-tail",
        "Add a route map, which drivers have asked for repeatedly.",
        Classification.SMALL,
    ),
    (
        "numbers",
        "Alert the manager when a vehicle is more than 5 days overdue for service.",
        Classification.SMALL,
    ),
    ("and-in-object", "Show the driver the pickup and drop-off addresses.", Classification.SMALL),
    (
        "hyphenated",
        "Let dispatchers bulk-close all completed jobs at end-of-day.",
        Classification.SMALL,
    ),
    ("apostrophe", "Show a driver's completed jobs for the week.", Classification.SMALL),
    (
        "long-single",
        "Allow an operations manager to generate a consolidated monthly reconciliation report for every vehicle in a selected depot that combines all recorded costs and events for the period before month-end close.",
        Classification.SMALL,
    ),
    (
        "two-actors",
        "Let dispatchers raise a fault and let drivers confirm the repair.",
        Classification.MEDIUM,
    ),
    (
        "nested-condition",
        "When a job is declined, notify the dispatcher who assigned it.",
        Classification.SMALL,
    ),
    (
        "semicolons",
        "Add depot filtering; add driver filtering; add date filtering.",
        Classification.MEDIUM,
    ),
    ("mixed-case", "ALLOW DRIVERS TO MARK A DELIVERY COMPLETE.", Classification.SMALL),
    (
        "extra-whitespace",
        "   Allow   drivers   to   scan   a   parcel   barcode.   ",
        Classification.SMALL,
    ),
    ("unicode-quotes", "Let drivers add a “note” to a delivery.", Classification.SMALL),
    ("emoji", "Allow drivers to react to a job with \U0001f44d.", Classification.SMALL),
    ("short-valid", "Add depot filtering to the job list.", Classification.SMALL),
    ("repeated-noun", "Allow drivers to report a report of damage.", Classification.SMALL),
]

IDS = [c[0] for c in CORPUS]


@pytest.mark.parametrize("label,requirement,expected", CORPUS, ids=IDS)
def test_every_shape_is_accepted(label, requirement, expected):
    """No usable requirement may be rejected as invalid."""
    verdict = validate_input(requirement, CTX)
    assert verdict.may_proceed, f"{label}: rejected as {verdict.status.value} — {verdict.reason}"


@pytest.mark.parametrize("label,requirement,expected", CORPUS, ids=IDS)
def test_every_shape_classifies_as_expected(label, requirement, expected):
    if expected is None:
        pytest.skip("classification not asserted for this shape")
    breakdown = heuristic_breakdown(requirement, CTX)
    assert breakdown.classification is expected, (
        f"{label}: got {breakdown.classification.value}, "
        f"capabilities={split_capabilities(requirement)}"
    )


@pytest.mark.parametrize("label,requirement,expected", CORPUS, ids=IDS)
def test_every_shape_produces_readable_stories(label, requirement, expected):
    """The output must read like English, whatever the input shape."""
    for story in heuristic_breakdown(requirement, CTX).stories:
        stmt = story.user_story_statement
        lowered = stmt.lower()

        after = lowered.split("i want", 1)[1].lstrip()
        assert after.startswith(("to ", "a ", "an ", "the ")), f"{label}: {stmt}"

        assert len(story.title.split()) >= 2, f"{label}: title too short — {story.title!r}"
        assert not story.title.lower().startswith(
            ("we ", "it would", "please", "can we", "i want", "as a")
        ), f"{label}: framing left in title — {story.title!r}"
        assert not story.title.isupper(), f"{label}: shouting title — {story.title!r}"

        actor = lowered.split(",")[0].replace("as an ", "").replace("as a ", "")
        assert not actor.startswith(("a ", "an ", "the ")), f"{label}: article in actor — {actor!r}"
        assert actor == actor.lower(), f"{label}: actor case — {actor!r}"
        if actor != GENERIC_ACTOR:
            # The generic fallback is deliberately four words; a *extracted*
            # actor should be a short noun phrase.
            assert len(actor.split()) <= 3, f"{label}: actor is a phrase — {actor!r}"


@pytest.mark.parametrize("label,requirement,expected", CORPUS, ids=IDS)
def test_every_shape_produces_valid_subtasks(label, requirement, expected):
    for story in heuristic_breakdown(requirement, CTX).stories:
        assert story.subtasks, f"{label}: story with no subtasks"
        for sub in story.subtasks:
            assert len(sub.title) <= 255
            assert len(sub.title.split()) >= 3, f"{label}: {sub.title!r}"


# --- shapes that SHOULD be refused ---------------------------------------

REJECT = [
    ("empty", ""),
    ("whitespace", "     "),
    ("gibberish", "asdfgh qwerty zxcvbnm hjkl"),
    ("keyboard-run", "asdfasdf"),
    ("repeated", "test test test test test test"),
    ("too-short", "Add photo"),
    ("digits-only", "12345 67890"),
    ("punctuation", "!!! ??? ..."),
]


@pytest.mark.parametrize("label,text", REJECT, ids=[r[0] for r in REJECT])
def test_unusable_input_is_refused(label, text):
    assert not validate_input(text, CTX).may_proceed, f"{label}: wrongly accepted"


# --- the parser must never raise -----------------------------------------

HOSTILE = [
    "",
    " ",
    "\n\n\n",
    ",,,,",
    "and and and",
    "to to to",
    "a",
    "A" * 500,
    "Allow " * 200,
    "...",
    "—",
    "\t\t",
    "🚚🚚🚚",
    "<script>alert(1)</script>",
    "SELECT * FROM issues;",
    "{'a': 1}",
    "\\x00 null byte",
    "café naïve résumé",
    "日本語のテキスト",
    "Allow, , , drivers to,,, do a thing",
]


@pytest.mark.parametrize("text", HOSTILE, ids=range(len(HOSTILE)))
def test_parser_never_raises(text):
    """Whatever arrives, the pipeline must return a verdict — never crash."""
    verdict = validate_input(text, CTX)
    if verdict.may_proceed:
        breakdown = heuristic_breakdown(text, CTX)
        assert breakdown.stories
    split_capabilities(text)
    extract_actor(text)
