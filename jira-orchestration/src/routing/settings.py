"""Everything this router reads from the environment, in one place.

**Activity-side only.** A Temporal workflow may not read the environment — that
is `RestrictedWorkflowAccessError`, and it has taken this system's sibling down
before. Every function here is called from a `@tool`, and the answer is handed
to the workflow as data.

The readers themselves are shared (`shared.settings`); what lives here is which
variables this agent has and what they default to.
"""

from __future__ import annotations

import os
import re
from typing import Any

from shared.settings import env_bool, env_int, env_list, secret

DEFAULT_TRIGGER_KEYWORD = "Aetherion"
DEFAULT_PROCESSED_LABEL = "aetherion-processed"
DEFAULT_CLASSIFIER_MODEL = "gpt-5.1"

# Where the router remembers which comments it has already answered. A Jira
# issue property is storage Jira provides for exactly this: invisible to users,
# attached to the issue, durable across restarts.
ANSWERED_PROPERTY = "aetherion-answered-comments"

# Where each ticket's orchestration state lives: the job running on it, and
# the last review and build results (routing/orchestration.py).
STATE_PROPERTY = "aetherion-orchestration"


def trigger_keyword() -> str:
    return os.environ.get("ORCH_TRIGGER_KEYWORD", "").strip() or DEFAULT_TRIGGER_KEYWORD


def processed_label() -> str:
    return os.environ.get("ORCH_PROCESSED_LABEL", "").strip() or DEFAULT_PROCESSED_LABEL


def classifier_model() -> str:
    return os.environ.get("ORCH_CLASSIFIER_MODEL", "").strip() or DEFAULT_CLASSIFIER_MODEL


# Model-id prefix -> AI Gateway provider, the same table the review agent uses.
# The gateway takes provider and model separately and does not infer one.
_PROVIDER_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("anthropic", ("claude-",)),
    ("gemini", ("gemini-",)),
    ("bedrock", ("amazon.", "anthropic.", "meta.", "us.", "eu.", "ap.")),
    ("openai", ("gpt-", "o1-", "o3-", "o4-", "chatgpt-")),
)


def provider_for_model(model: str) -> str:
    """``ORCH_CLASSIFIER_PROVIDER`` if set, else inferred from the model id."""
    override = os.environ.get("ORCH_CLASSIFIER_PROVIDER", "").strip().lower()
    if override:
        return override
    lowered = model.strip().lower()
    for provider, prefixes in _PROVIDER_PREFIXES:
        if lowered.startswith(prefixes):
            return provider
    return "openai"


def bot_account_id() -> str:
    """The Jira account this system posts as, when it is known.

    Optional: the signature check works without it. Setting it makes the
    self-guard cheaper and immune to someone quoting our reply.
    """
    return os.environ.get("ORCH_BOT_ACCOUNT_ID", "").strip()


def notify_emails() -> tuple[str, ...]:
    return tuple(env_list("ORCH_NOTIFY_EMAILS"))


# The outcomes that email, by the same names the work-breakdown agent uses in
# TASK_NOTIFY_ON, so one list means the same thing in both deployments.
NOTIFY_KINDS = ("created", "clarification", "duplicates", "failed")


def email_off() -> bool:
    """``ORCH_NOTIFY_ON=none``: no email at all — replies on the ticket only (2026-10-08).

    Outcome emails and the administrators' alerts both stop; alerts are still logged.
    """
    return "none" in {k.lower() for k in env_list("ORCH_NOTIFY_ON")}


def notify_on() -> tuple[str, ...]:
    """``ORCH_NOTIFY_ON``, or all four. Unknown names are ignored, not guessed."""
    if email_off():
        return ()
    chosen = tuple(k.lower() for k in env_list("ORCH_NOTIFY_ON") if k.lower() in NOTIFY_KINDS)
    return chosen or NOTIFY_KINDS


def email_repeat_window_seconds() -> int:
    """Seconds an identical email is suppressed for, so a retry cannot flood."""
    return env_int("ORCH_EMAIL_REPEAT_WINDOW", 900, minimum=0)


def allowed_project_keys() -> tuple[str, ...]:
    """Projects this deployment may answer in.

    Empty means any project the token can reach. Setting it caps the blast
    radius once one published router serves several teams.
    """
    return tuple(k.upper() for k in env_list("ORCH_ALLOWED_PROJECT_KEYS"))


def read_only() -> bool:
    """When true the router composes replies but posts nothing.

    For watching what it *would* say on a live project without touching it.
    """
    return env_bool("ORCH_READ_ONLY")


def task_queue_overrides() -> dict[str, str]:
    """Per-agent task queue overrides, keyed by registered agent name.

    Each agent's worker polls a queue assigned by the platform at publish
    time, which is not knowable from source. Without an explicit queue,
    dispatching a child workflow defaults to the CALLER's queue — the
    router's own — and the child fails immediately because that worker never
    registered its workflow type. Read here, in the activity, and handed to
    the workflow as plain data — never read from the workflow itself.
    """
    overrides = {
        "JiraTaskCreation": os.environ.get("ORCH_TASK_QUEUE_JIRA_TASK_CREATION", "").strip(),
        "JiraRequirementReview": os.environ.get(
            "ORCH_TASK_QUEUE_JIRA_REQUIREMENT_REVIEW", ""
        ).strip(),
    }
    return {name: queue for name, queue in overrides.items() if queue}


def jira_credentials() -> tuple[str, str, str]:
    base = os.environ.get("JIRA_BASE_URL", "").strip().rstrip("/")
    email = os.environ.get("JIRA_EMAIL", "").strip()
    # A secret: a mounted file in production, the variable on a laptop (2026-10-15).
    token = secret("JIRA_API_TOKEN")
    return base, email, token


def build_min_readiness() -> int:
    """A build after a review scored below this (1-5) is paused, not built.

    3: a "needs major clarification" review (1-2) means the tickets would be
    thin and someone would have to redo them. Only a review of the unchanged
    ticket counts — edit the description and the build runs.
    """
    return env_int("ORCH_BUILD_MIN_READINESS", 3, minimum=0)


def status_labels() -> bool:
    """Show what the agent is doing on the board card, as one label at a time."""
    return env_bool("ORCH_STATUS_LABELS", default=True)


def queue_wait_minutes() -> int:
    """How long a queued request waits for the job ahead of it on its ticket."""
    return env_int("ORCH_QUEUE_WAIT_MINUTES", 10, minimum=1)


# --- GitHub → Planning (2026-10-03) ---------------------------------------------

# Where the router remembers how far it has read GitHub, and what it has
# already passed to the Planning agent: a property on a Jira project, so no
# new storage is needed. Invisible to users.
GITHUB_STATE_PROPERTY = "aetherion-github-poll"
DEFAULT_GITHUB_BRANCH = "main"
DEFAULT_PLANNING_AGENT = "planning_agent"
DEFAULT_PLANNING_TIMEOUT_MINUTES = 15
DEFAULT_GITHUB_MAX_TRIES = 5


def github_token() -> str:
    """A fine-grained token for the organization's repositories."""
    return secret("GITHUB_TOKEN")


def github_org() -> str:
    return os.environ.get("ORCH_GITHUB_ORG", "").strip()


def github_branch() -> str:
    return os.environ.get("ORCH_GITHUB_BRANCH", "").strip() or DEFAULT_GITHUB_BRANCH


def github_repos() -> tuple[str, ...]:
    """Repos to watch, by name; empty means every repo of the organization."""
    return tuple(r.lower() for r in env_list("ORCH_GITHUB_REPOS"))


def github_state_project() -> str:
    """The Jira project holding the memory; the first allowed project by default."""
    chosen = os.environ.get("ORCH_GITHUB_STATE_PROJECT", "").strip().upper()
    return chosen or next(iter(allowed_project_keys()), "")


def planning_agent() -> str:
    return os.environ.get("ORCH_PLANNING_AGENT", "").strip() or DEFAULT_PLANNING_AGENT


def planning_task_queue() -> str:
    """The Planning agent's queue; empty lets the platform's agent map decide."""
    return os.environ.get("ORCH_TASK_QUEUE_PLANNING", "").strip()


def planning_timeout_minutes() -> int:
    return env_int("ORCH_PLANNING_TIMEOUT_MINUTES", DEFAULT_PLANNING_TIMEOUT_MINUTES, minimum=1)


def github_max_tries() -> int:
    return env_int("ORCH_GITHUB_MAX_TRIES", DEFAULT_GITHUB_MAX_TRIES, minimum=1)


# --- After a build: Review -> Planning -> Coding (2026-10-08) -----------------------

PIPELINE_STEPS = ("review", "plan", "code")
DEFAULT_CODING_AGENT = "coding-agent"
# Coding's own limit is 30 minutes (its change, then up to 10 for the PR's CI).
DEFAULT_CODING_TIMEOUT_MINUTES = 35
DEFAULT_CODING_ATTEMPTS = 2


def build_pipeline() -> tuple[str, ...]:
    """What follows a build, in order: some of review, plan, code (empty = nothing).

    Coding implements Planning's design, so "code" without "plan" is dropped.
    """
    chosen = {s.lower() for s in env_list("ORCH_BUILD_PIPELINE")}
    steps = tuple(s for s in PIPELINE_STEPS if s in chosen)
    return tuple(s for s in steps if s != "code" or "plan" in steps)


def planning_repos() -> str:
    """The services Planning designs against, one "name: org/repo[@branch]" per line.

    One environment line, so entries may also be separated by ";" or ",".
    """
    raw = os.environ.get("ORCH_PLANNING_REPOS", "")
    entries = [e.strip() for e in re.split(r"[\n;,]", raw) if e.strip()]
    return "\n".join(entries)


def planning_uses_org() -> bool:
    """Every repository of ORCH_GITHUB_ORG is designed and coded against (2026-10-16),
    a new one as soon as it exists — ORCH_PLANNING_REPOS then only names some of
    them (or adds repositories outside the organization). ORCH_PLANNING_ORG_REPOS=false
    goes back to the list alone."""
    flag = os.environ.get("ORCH_PLANNING_ORG_REPOS", "").strip().lower()
    return bool(github_org()) and flag not in ("false", "0", "no", "off")


def coding_agent() -> str:
    return os.environ.get("ORCH_CODING_AGENT", "").strip() or DEFAULT_CODING_AGENT


def coding_task_queue() -> str:
    """The Coding agent's queue; empty lets the platform's agent map decide."""
    return os.environ.get("ORCH_TASK_QUEUE_CODING", "").strip()


def coding_timeout_minutes() -> int:
    return env_int("ORCH_CODING_TIMEOUT_MINUTES", DEFAULT_CODING_TIMEOUT_MINUTES, minimum=1)


def coding_attempts() -> int:
    """Coding runs at most this often; each retry first sends its failure back to Planning."""
    return env_int("ORCH_CODING_ATTEMPTS", DEFAULT_CODING_ATTEMPTS, minimum=1)


def plan_min_readiness() -> int:
    """Planning waits when the review scored below this (1-5); 0 never waits."""
    return env_int("ORCH_PLAN_MIN_READINESS", 0, minimum=0)


def storage_bucket() -> str:
    """The storage the agents share, where Planning keeps its designs (set by the platform)."""
    return os.environ.get("TENANT_ID", "").strip() or os.environ.get("STORAGE_AGENTS_BUCKET", "")


def pipeline() -> dict[str, object]:
    """Everything the workflow needs to run the steps after a build, as data."""
    return {
        "steps": list(build_pipeline()),
        "repos": planning_repos(),
        "planning_agent": planning_agent(),
        "planning_queue": planning_task_queue(),
        "planning_timeout_minutes": planning_timeout_minutes(),
        "coding_agent": coding_agent(),
        "coding_queue": coding_task_queue(),
        "coding_timeout_minutes": coding_timeout_minutes(),
        "coding_attempts": coding_attempts(),
        "plan_min_readiness": plan_min_readiness(),
    }


# --- A request from the orchestrator's own form (2026-10-08) -------------------------

DEFAULT_REQUEST_MIN_READINESS = 4


def request_project() -> str:
    """The Jira project a form request is checked against for duplicates (read only).

    ``ORCH_REQUEST_PROJECT``, else the first allowed project.
    """
    chosen = os.environ.get("ORCH_REQUEST_PROJECT", "").strip().upper()
    return chosen or next(iter(allowed_project_keys()), "")


def request_min_readiness() -> int:
    """The review score a form request needs before Planning (the form may set it)."""
    return env_int("ORCH_REQUEST_MIN_READINESS", DEFAULT_REQUEST_MIN_READINESS, minimum=0)


def local_document_dir() -> str:
    """Where a laptop run keeps a copy of each design (set by scripts/run_local.sh)."""
    return os.environ.get("ORCH_DOCUMENT_DIR", "").strip()


def local_run() -> bool:
    """True only on a laptop run (set by scripts/local_worker.py, never in a .env)."""
    return os.environ.get("AETHERION_LOCAL_RUN", "") == "1"


# --- Confluence: the TDD and its discussion (2026-10-08) -----------------------------

DEFAULT_TDD_LABEL = "aetherion-tdd"
TDD_PROPERTY = "aetherion-tdd"  # on the Jira story: which page is its TDD
STORY_PROPERTY = "aetherion-story"  # on the Confluence page: which story it designs
ANSWERED_PAGE_PROPERTY = "aetherion-answered-comments"  # on the page: comments answered
CODE_PLAN_PROPERTY = "aetherion-code-plan"  # on the page: branches proposed / confirmed


def confluence_base() -> str:
    """Confluence's address: ``ORCH_CONFLUENCE_URL``, else the Jira site's ``/wiki``."""
    chosen = os.environ.get("ORCH_CONFLUENCE_URL", "").strip().rstrip("/")
    if chosen:
        return chosen
    base, _, _ = jira_credentials()
    return f"{base}/wiki" if base else ""


def confluence_credentials() -> tuple[str, str]:
    """The Confluence login: its own when set, else the Jira login (same Atlassian site)."""
    _, email, token = jira_credentials()
    return (
        os.environ.get("CONFLUENCE_EMAIL", "").strip() or email,
        secret("CONFLUENCE_API_TOKEN") or token,
    )


def confluence_spaces() -> dict[str, str]:
    """``ORCH_CONFLUENCE_SPACES``: project -> space key, e.g. "BGV:PR,FL:FL".

    Unset projects use the space whose key is the project key or whose name is
    the project's name, and get a new space when there is none.
    """
    out: dict[str, str] = {}
    for item in env_list("ORCH_CONFLUENCE_SPACES"):
        project, _, space = item.partition(":")
        if project.strip() and space.strip():
            out[project.strip().upper()] = space.strip()
    return out


def tdd_label() -> str:
    return os.environ.get("ORCH_TDD_LABEL", "").strip() or DEFAULT_TDD_LABEL


def tdd_min_readiness() -> int:
    """The review score (1-5) a story needs before its TDD is written."""
    return env_int("ORCH_TDD_MIN_READINESS", 4, minimum=0)


def memory_enabled() -> bool:
    """``ORCH_MEMORY`` (2026-10-26): "on" (default) asks Planning whether a story's
    requirement changed and checks every change to a TDD; "off" is the behaviour
    before that (any text change rewrites, every update is applied)."""
    return os.environ.get("ORCH_MEMORY", "").strip().lower() not in ("off", "false", "0", "no")


def handoff_enabled() -> bool:
    """``ORCH_HANDOFF`` (2026-10-29): "on" (default, with ``ORCH_MEMORY`` on) reads
    the story once per command and keeps the card's memory in Memsy
    (Orchestration alone, 2026-10-30): the review's questions with their
    answers, earlier cards about the same thing, the services the card touches
    ranked, and each card's history (TDD, pull requests, merges). The review is
    given that memory; Planning gets the agreed input. "off" is the behaviour
    before."""
    return os.environ.get("ORCH_HANDOFF", "").strip().lower() not in ("off", "false", "0", "no")


def commands() -> dict[str, object]:
    """What the TDD, design and code commands run with, as data for the workflow."""
    return {
        "repos": planning_repos(),
        "planning_agent": planning_agent(),
        "planning_queue": planning_task_queue(),
        "planning_timeout_minutes": planning_timeout_minutes(),
        "coding_agent": coding_agent(),
        "coding_queue": coding_task_queue(),
        "coding_timeout_minutes": coding_timeout_minutes(),
        "coding_attempts": coding_attempts(),
        "tdd_min_readiness": tdd_min_readiness(),
        "task_queue_overrides": task_queue_overrides(),
        "memory": memory_enabled(),
        "handoff": memory_enabled() and handoff_enabled(),
    }


# --- the webhooks that push events here (2026-10-15) -----------------------------

# What each webhook needs to be live, besides its URL (WORKFLOW.md, "Webhooks").
WEBHOOK_CHECKS = {
    "jira": "Jira webhook → comments on stories (works today)",
    "confluence": "Confluence Automation rule → comments on TDD pages (replaces the poll)",
    "github": "GitHub webhook → merges and PR comments (replaces the poll)",
}


def webhook_urls() -> dict[str, str]:
    """Where Jira, Confluence and GitHub post their events — or {} until the
    platform's webhook address is known.

    ORCH_WEBHOOK_BASE_URL  e.g. https://wh.sbox.aetherion.io/webhooks/v2
    ORCH_WEBHOOK_TENANT_ID the tenant's id (the Jira webhook URL's tenant_id)
    ORCH_WEBHOOK_TENANT    the tenant's name, e.g. test (optional)
    """
    base = os.environ.get("ORCH_WEBHOOK_BASE_URL", "").strip().rstrip("/")
    tenant_id = os.environ.get("ORCH_WEBHOOK_TENANT_ID", "").strip()
    tenant = os.environ.get("ORCH_WEBHOOK_TENANT", "").strip()
    if not (base and tenant_id):
        return {}
    query = f"?tenant_id={tenant_id}" + (f"&tenant={tenant}" if tenant else "")
    return {provider: f"{base}/{provider}{query}" for provider in WEBHOOK_CHECKS}


def webhook_status() -> dict[str, Any]:
    """What the health check reports about the webhooks: their URLs, or which
    checks wait for the address."""
    urls = webhook_urls()
    if urls:
        return {"configured": True, "urls": urls}
    return {
        "configured": False,
        "missing": "ORCH_WEBHOOK_BASE_URL and ORCH_WEBHOOK_TENANT_ID",
        "waiting": list(WEBHOOK_CHECKS.values()),
    }


def alert_webhook_url() -> str:
    """ORCH_ALERT_WEBHOOK_URL (2026-10-15): a chat channel's incoming webhook (Slack,
    Teams, …) the administrators' alerts are posted to — whatever the email setting:
    with email off, alerts still need to reach a person."""
    return os.environ.get("ORCH_ALERT_WEBHOOK_URL", "").strip()
