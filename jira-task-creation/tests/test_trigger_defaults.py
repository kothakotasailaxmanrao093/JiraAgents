"""The trigger form and the code must agree about what the defaults are.

The sibling agent (jira-requirement-review) had exactly this bug: a boolean
trigger with no ``"default"`` in metadata.json renders **unchecked**, whatever
its help text claims, and nothing fails — the run is just thinner than
intended (FLAWS.md F5). This agent's two booleans, ``create_in_jira`` and
``force``, currently agree between metadata.json and the code, but nothing
was pinning that agreement, which is the same class of silent gap.

These tests pin both halves: the declared defaults are the single set in
``agent.TRIGGER_DEFAULTS``, and the metadata the platform reads carries the
same values.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.agent.agent import TRIGGER_DEFAULTS, _as_bool

METADATA = Path(__file__).resolve().parents[1] / "src" / "agent" / "metadata.json"


def _triggers() -> dict[str, dict]:
    data = json.loads(METADATA.read_text())
    return {t["name"]: t for t in data["config"]["triggers"]}


def test_every_boolean_trigger_declares_a_default() -> None:
    """A boolean with no ``"default"`` renders unchecked, whatever its help text
    claims. That mismatch is the entire bug — see FLAWS.md F5."""
    missing = [
        name
        for name, trigger in _triggers().items()
        if trigger.get("type") == "boolean" and "default" not in trigger
    ]
    assert missing == [], (
        f'These boolean triggers have no "default" in metadata.json: {missing}. '
        "The form will render them unchecked and submit false."
    )


def test_the_metadata_defaults_match_the_code() -> None:
    """One definition, asserted in both places — this is the drift guard."""
    triggers = _triggers()
    for name, expected in sorted(TRIGGER_DEFAULTS.items()):
        assert name in triggers, f"{name} is in TRIGGER_DEFAULTS but not in metadata.json"
        actual = triggers[name].get("default")
        assert actual == expected, (
            f"{name}: metadata.json says {actual!r}, TRIGGER_DEFAULTS says "
            f"{expected!r}. Change TRIGGER_DEFAULTS and re-run — they must agree."
        )


def test_every_boolean_in_the_metadata_is_declared_in_the_code() -> None:
    """A new boolean trigger must be added to TRIGGER_DEFAULTS, or its default
    exists only in the form and nothing in the code knows about it."""
    undeclared = [
        name
        for name, trigger in _triggers().items()
        if trigger.get("type") == "boolean" and name not in TRIGGER_DEFAULTS
    ]
    assert undeclared == [], f"Add these to agent.TRIGGER_DEFAULTS: {undeclared}"


def test_the_help_text_matches_the_declared_default() -> None:
    """The description a user reads must not contradict the box they are shown.

    "default true"/"(default)" next to an unchecked box is how F5 was noticed
    on the sibling agent.
    """
    for name, trigger in sorted(_triggers().items()):
        if trigger.get("type") != "boolean":
            continue
        text = trigger.get("description", "").lower()
        declared = trigger.get("default")
        if "default true" in text or "default on" in text or "(default)" in text:
            assert (
                declared is True
            ), f"{name}: help text implies default true, metadata says {declared}"
        if "default off" in text or "default false" in text:
            assert (
                declared is False
            ), f"{name}: help text says default off, metadata says {declared}"


# --- how the code reads a flag, including the blank-field case --------------


def test_an_absent_key_falls_back_to_the_declared_default() -> None:
    assert _as_bool(None, default=TRIGGER_DEFAULTS["create_in_jira"]) is True
    assert _as_bool(None, default=TRIGGER_DEFAULTS["force"]) is False


def test_a_blank_field_falls_back_to_the_declared_default() -> None:
    """Blank fields arrive from the local form as empty strings, not absent
    keys — the exact shape that broke the sibling agent (F5)."""
    assert _as_bool("", default=TRIGGER_DEFAULTS["create_in_jira"]) is True
    assert _as_bool("", default=TRIGGER_DEFAULTS["force"]) is False


def test_an_explicit_value_always_wins() -> None:
    assert _as_bool(False, default=TRIGGER_DEFAULTS["create_in_jira"]) is False
    assert _as_bool(True, default=TRIGGER_DEFAULTS["force"]) is True
