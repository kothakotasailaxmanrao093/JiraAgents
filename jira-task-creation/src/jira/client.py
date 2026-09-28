"""Jira Cloud REST v3 integration for the work-breakdown agent.

Follows the conventions already established in the sibling ``asurint`` project:
credentials from the environment, one pooled ``httpx.AsyncClient`` per call,
secrets only ever logged through ``_mask``, JQL string values escaped, and
failures returned as data rather than raised across the activity boundary.

Two behaviours are specific to this agent:

* **Preflight.** The Jira project and the issue types the breakdown needs are
  verified *before* the first write, so a missing Sub-task type fails with
  nothing created rather than half a hierarchy.
* **Idempotency.** Every hierarchy carries a deterministic label derived from
  the requirement text and target project. A retry finds that label and returns
  the existing issues instead of creating duplicates.
"""

from __future__ import annotations

import os

import httpx
from common_lib.utils.logger import setup_logger

from src.config.settings import env_bool, env_list, mask

logger = setup_logger(__name__)

IDEMPOTENCY_LABEL_PREFIX = "ltw"
_DEFAULT_TIMEOUT = 30.0


# --------------------------------------------------------------------------
# Credentials and low-level helpers (mirrors asurint/src/tools/tools.py)
# --------------------------------------------------------------------------


def _mask(secret: str) -> str:
    """Show only the shape of a secret in logs — never the value.

    Three characters at each end, which is enough to tell two Jira tokens apart
    in a log without being enough to use one.
    """
    return mask(secret, reveal=3)


def _quote(value: str) -> str:
    """Quote a JQL string value, escaping backslashes and double quotes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def jira_creds() -> tuple[str, str, str]:
    """Read Jira base URL + credentials from the environment."""
    base_url = os.environ.get("JIRA_BASE_URL", "").strip().rstrip("/")
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    return base_url, email, token


def jira_client(base_url: str, email: str, token: str) -> httpx.AsyncClient:
    """Build a Jira HTTP client with base URL + auth baked in."""
    return httpx.AsyncClient(
        base_url=base_url,
        auth=(email, token),
        timeout=_DEFAULT_TIMEOUT,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )


def issue_type_names() -> dict[str, str]:
    """Issue type names, overridable per Jira instance."""
    return {
        "epic": os.environ.get("JIRA_EPIC_ISSUE_TYPE", "Epic").strip() or "Epic",
        "story": os.environ.get("JIRA_STORY_ISSUE_TYPE", "Story").strip() or "Story",
        "subtask": os.environ.get("JIRA_SUBTASK_ISSUE_TYPE", "Sub-task").strip() or "Sub-task",
    }


# Company-managed projects call it "Sub-task"; team-managed ones "Subtask". The
# BGV project was recreated as team-managed and every build failed with
# "does not offer the required issue type(s): Sub-task" (2026-09-27).
_ALIASES = {
    "subtask": ("sub-task", "subtask", "sub task"),
    "story": ("story", "user story"),
    "epic": ("epic",),
}


def resolve_issue_types(available: list[str]) -> dict[str, str]:
    """The configured type names, mapped to what THIS project actually offers.

    The configured name wins when the project has it; otherwise another
    spelling of the SAME default name is used ("Sub-task" <-> "Subtask"). A
    deliberately custom name ("Initiative", "UserStory") never falls back to a
    default — it is returned as configured, so ``missing_issue_types`` reports it.
    """
    configured = issue_type_names()
    offered = {name.strip().lower(): name for name in available}
    resolved: dict[str, str] = {}
    for role, name in configured.items():
        if name.strip().lower() in offered:
            resolved[role] = offered[name.strip().lower()]
            continue
        spellings = _ALIASES.get(role, ())
        alias = None
        if name.strip().lower() in spellings:
            alias = next((offered[a] for a in spellings if a in offered), None)
        resolved[role] = alias or name
    return resolved


def target_project_key(override: str = "") -> str:
    return (override or os.environ.get("JIRA_PROJECT_KEY", "")).strip().upper()


def allowed_project_keys() -> tuple[str, ...]:
    """Projects this deployment may write to, from ``LTW_ALLOWED_PROJECT_KEYS``.

    Empty means "any project the token can reach", which is the historical
    behaviour. Setting it caps the blast radius once several people share one
    published agent and one service account: a mistyped or deliberately
    different key cannot reach a project you did not name.
    """
    return tuple(k.upper() for k in env_list("LTW_ALLOWED_PROJECT_KEYS"))


def read_only() -> bool:
    """When true the agent refuses every write, whatever the caller asked for."""
    return env_bool("LTW_READ_ONLY")


def browse_url(base_url: str, key: str) -> str:
    return f"{base_url}/browse/{key}" if base_url and key else ""


def _http_error_text(exc: httpx.HTTPStatusError) -> str:
    return f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


async def available_issue_types(client: httpx.AsyncClient, project_key: str) -> list[str]:
    """Names of the issue types creatable in ``project_key``."""
    resp = await client.get(
        f"/rest/api/3/issue/createmeta/{project_key}/issuetypes",
        params={"maxResults": 100},
    )
    resp.raise_for_status()
    payload = resp.json()
    values = payload.get("issueTypes") or payload.get("values") or []
    return [str(v.get("name", "")) for v in values if v.get("name")]


def missing_issue_types(needed: list[str], available: list[str]) -> list[str]:
    """Which of ``needed`` are absent from ``available`` (case-insensitive)."""
    have = {name.strip().lower() for name in available}
    return [name for name in needed if name.strip().lower() not in have]
