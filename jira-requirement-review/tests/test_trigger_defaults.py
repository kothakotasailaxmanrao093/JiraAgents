"""The trigger form and the code must agree about what the defaults are.

They did not, and the failure was silent. `metadata.json` declared every boolean
trigger with no `"default"` key, so the platform rendered each one as an
**unchecked** box and submitted an explicit `false`. The code's
`payload.get("include_parent", True)` only applies when the key is *absent*, so
a UI launch quietly read less context than the help text promised ("default
true") — and "Publish to Jira" contradicted its own "Default OFF".

Nothing failed. The review was just thinner than it should have been, which
looks exactly like a badly written ticket.

These tests pin both halves: the declared defaults are the single set in
`config.TRIGGER_DEFAULTS`, and the metadata the platform reads carries the same
values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import TRIGGER_DEFAULTS, flag

METADATA = Path(__file__).resolve().parents[1] / "src" / "agent" / "metadata.json"


def _triggers() -> dict[str, dict]:
    data = json.loads(METADATA.read_text())
    return {t["name"]: t for t in data["config"]["triggers"]}


def test_every_boolean_trigger_declares_a_default() -> None:
    """A boolean with no ``"default"`` renders unchecked, whatever its help text
    claims. That mismatch is the entire bug."""
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
            f"{name}: metadata.json says {actual!r}, config.TRIGGER_DEFAULTS says "
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
    assert undeclared == [], f"Add these to config.TRIGGER_DEFAULTS: {undeclared}"


def test_the_help_text_matches_the_declared_default() -> None:
    """The description a user reads must not contradict the box they are shown.

    "default true" next to an unchecked box is how this was noticed.
    """
    for name, trigger in sorted(_triggers().items()):
        if trigger.get("type") != "boolean":
            continue
        text = trigger.get("description", "").lower()
        declared = trigger.get("default")
        if "default true" in text or "default on" in text:
            assert (
                declared is True
            ), f"{name}: help text says default true, metadata says {declared}"
        if "default off" in text or "default false" in text:
            assert (
                declared is False
            ), f"{name}: help text says default off, metadata says {declared}"


# --- how the code reads a flag ----------------------------------------------


def test_an_absent_key_falls_back_to_the_declared_default() -> None:
    assert flag({}, "include_parent") is True
    assert flag({}, "post_to_jira") is False


def test_an_explicit_value_always_wins() -> None:
    """The form submits explicitly, so this is the normal path, not the edge."""
    assert flag({"include_parent": False}, "include_parent") is False
    assert flag({"post_to_jira": True}, "post_to_jira") is True


def test_an_explicit_none_is_treated_as_absent() -> None:
    """Some callers send every key, with null for "not set"."""
    assert flag({"include_parent": None}, "include_parent") is True


@pytest.mark.parametrize("value,expected", [(1, True), (0, False), ("", False), ("x", True)])
def test_truthy_values_are_coerced(value: object, expected: bool) -> None:
    assert flag({"generate_docx": value}, "generate_docx") is expected


def test_an_undeclared_flag_is_a_programming_error() -> None:
    """Fail loudly rather than inventing a default at the call site."""
    with pytest.raises(KeyError):
        flag({}, "not_a_real_trigger")
