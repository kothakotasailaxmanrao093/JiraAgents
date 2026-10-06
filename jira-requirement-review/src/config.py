"""Centralized configuration for the Jira Requirement Review agents.

All environment access lives here so it is consistent and testable, and so
secrets are read in exactly one place. Mirrors the convention in
``asurint/my_first_agent``: read from the environment, never hardcode secrets,
and expose ``mask()`` so credential *state* can be logged without leaking values.

This module is framework-free (stdlib only) so it can be imported by the pure
layers and unit-tested without the Aetherion runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from shared.settings import mask as _mask

# ---------------------------------------------------------------------------
# Size caps — protect the workflow engine's ~500KB per-message payload limit and
# bound LLM token cost. Tunable in one place.
# ---------------------------------------------------------------------------
MAX_SUBTASKS = 15
MAX_LINKED_ISSUES = 15
MAX_COMMENTS_PER_ISSUE = 20
MAX_CHARS_PER_COMMENT = 1000
MAX_DESC_CHARS = 8000
MAX_TRANSCRIPT_CHARS = 24000
# Cap on issues reviewed per JQL/Sprint batch run — each issue gets the full
# per-issue pipeline (parent + subtasks + linked issues + an LLM call), so this
# bounds both wall-clock time and LLM cost for a single batch invocation.
MAX_JQL_RESULTS = 25
# Aggregate budget across ALL gathered documents. Per-field caps above bound each
# field, but only this total bounds the combined payload that crosses the
# workflow<->activity boundary; kept well under the ~500KB engine message limit.
MAX_TOTAL_CONTEXT_CHARS = 200000

# Attachments and Confluence: the specification is routinely a file or a linked
# page rather than prose on the card. Each file costs a download plus an
# extraction, and images additionally cost a vision call, so both are capped.
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_CHARS = 20000
READ_ATTACHMENT_IMAGES = True
MAX_CONFLUENCE_PAGES = 5
MAX_CONFLUENCE_CHARS = 20000

DEFAULT_MODEL_ID = "gpt-5.1"


# ---------------------------------------------------------------------------
# Trigger defaults — ONE definition, used by the code and asserted against
# src/agent/metadata.json by tests/test_trigger_defaults.py.
#
# These have to exist in both places, and they drifted: metadata.json declared
# the booleans with no ``"default"`` key, so the platform rendered every one as
# an UNCHECKED box and submitted an explicit ``false``. The code's
# ``payload.get("include_parent", True)`` only applies when the key is ABSENT,
# so a UI launch silently read less than the help text promised ("default true")
# while "Publish to Jira" disagreed with its own "Default OFF".
#
# Adding ``"default"`` to the metadata fixes what the form sends; keeping the
# values here fixes what the code assumes. The test fails if they ever disagree
# again.
# ---------------------------------------------------------------------------
TRIGGER_DEFAULTS: dict[str, bool] = {
    # Read the surrounding context by default: a requirement is routinely
    # incomplete on its own card, and the parent or a sub-task holds the rest.
    "include_parent": True,
    "include_subtasks": True,
    "include_linked_issues": True,
    # Read the files hanging off the ticket, and the Confluence pages it links
    # to. Both default ON for the same reason the issue fields do: the
    # requirement is frequently only in there, and a review that never opened
    # the spec reads as a badly written ticket rather than an unread one.
    "include_attachments": True,
    "include_confluence": True,
    # Write nothing unless asked. This agent returns a draft; posting is opt-in,
    # and over the webhook path it is forced off entirely.
    "post_to_jira": False,
    "generate_docx": False,
}


def flag(payload: dict, name: str) -> bool:
    """One boolean trigger, honouring an explicit value and falling back to the
    single declared default.

    Reading through here rather than ``payload.get(name, True)`` at each call
    site is what keeps the default in one place.
    """
    if name not in TRIGGER_DEFAULTS:
        raise KeyError(f"{name!r} is not a declared trigger; add it to TRIGGER_DEFAULTS")
    value = payload.get(name)
    if value is None:
        return TRIGGER_DEFAULTS[name]
    return bool(value)


def mask(secret: str | None) -> str:
    """Show only the shape of a secret in logs — never the value.

    Three characters at each end: enough to tell two Jira tokens apart in a log,
    not enough to use one. The implementation is shared with the other Jira
    agents, which had a second copy revealing a different number of characters —
    so a change to how secrets are logged could be made in one and missed in the
    other.
    """
    return _mask(secret, reveal=3)


def resolve_team_id(payload_team_id: str | None = None) -> str | None:
    """Bucket / team id for object storage. Payload value wins, then ``TENANT_ID``.

    Returns ``None`` when neither is set — callers MUST then skip storage entirely
    (no bucket => no transcript file read, and nothing is stored).
    """
    if payload_team_id and payload_team_id.strip():
        return payload_team_id.strip()
    env = os.environ.get("TENANT_ID", "").strip()
    return env or None


def model_id_from_env(override: str | None = None) -> str:
    """Resolve the LLM model id: explicit ``override`` (payload), then the
    ``LLM_MODEL_ID`` / ``OPENAI_MODEL_ID`` env vars, then ``DEFAULT_MODEL_ID``.

    ``LLM_MODEL_ID`` is preferred (the model may be any provider, e.g. a Claude id);
    ``OPENAI_MODEL_ID`` is kept for backward compatibility.
    """
    if override and override.strip():
        return override.strip()
    for var in ("LLM_MODEL_ID", "OPENAI_MODEL_ID"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return DEFAULT_MODEL_ID


# Model-id prefix -> AI Gateway provider name (mirrors agent_lib's routing).
_PROVIDER_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("anthropic", ("claude-",)),
    ("gemini", ("gemini-",)),
    ("bedrock", ("amazon.", "anthropic.", "meta.", "us.", "eu.", "ap.")),
    ("openai", ("gpt-", "o1-", "o3-", "o4-", "chatgpt-")),
)

DEFAULT_PROVIDER = "openai"


def provider_from_model(model_id: str, override: str | None = None) -> str:
    """Resolve the AI Gateway provider for a model id.

    Precedence: explicit ``override`` -> ``LLM_PROVIDER`` env -> prefix detection
    -> ``openai``. The gateway takes provider + model_name separately, so we derive
    the provider here rather than letting a factory auto-detect it.
    """
    if override and override.strip():
        return override.strip().lower()
    env = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if env:
        return env
    mid = (model_id or "").lower()
    for provider, prefixes in _PROVIDER_PREFIXES:
        if any(mid.startswith(p) for p in prefixes):
            return provider
    return DEFAULT_PROVIDER


@dataclass(frozen=True)
class JiraSettings:
    """Validated Jira connection settings sourced from the environment."""

    base_url: str
    email: str
    api_token: str

    @staticmethod
    def from_env() -> JiraSettings:
        base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        email = os.environ.get("JIRA_EMAIL", "").strip()
        token = os.environ.get("JIRA_API_TOKEN", "").strip()
        missing = [
            name
            for name, value in (
                ("JIRA_BASE_URL", base),
                ("JIRA_EMAIL", email),
                ("JIRA_API_TOKEN", token),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"Missing required Jira env var(s): {', '.join(missing)}. "
                "Set JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN."
            )
        return JiraSettings(base_url=base, email=email, api_token=token)

    def __repr__(self) -> str:  # never leak the token, even in tracebacks/logs
        return (
            f"JiraSettings(base_url={self.base_url!r}, email={self.email!r}, "
            f"api_token={mask(self.api_token)})"
        )


# ---------------------------------------------------------------------------
# Agent identity — what the router puts in the reply's "Handled by" line
# ---------------------------------------------------------------------------

# The name the platform registers this agent under (see src/agent/metadata.json
# and the @agent decorator). The router dispatches on it.
AGENT_NAME = "JiraRequirementReview"

# Stable, human-readable, and never the class name. A person reading a Jira
# comment sees this, so it must stay recognisable across versions.
AGENT_DISPLAY_NAME = "JiraRequirementReview"

# Delegated mode runs inside a Temporal workflow, which may not read files or
# the environment — that is `RestrictedWorkflowAccessError`. So the version is a
# literal, NOT a read of metadata.json: reading the file would be workflow I/O
# on every run.
#
# Kept in step by scripts/publish.py, which writes all three (pyproject,
# metadata.json and this constant) together, and by
# tests/test_version_agreement.py, which fails if they ever diverge.
AGENT_VERSION = "2.2.22"


def agent_version() -> str:
    """This build's version. A constant — see AGENT_VERSION above for why."""
    return AGENT_VERSION
