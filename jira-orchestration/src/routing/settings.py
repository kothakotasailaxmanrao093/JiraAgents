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

from shared.settings import env_bool, env_int, env_list

DEFAULT_TRIGGER_KEYWORD = "Aetherion"
DEFAULT_PROCESSED_LABEL = "aetherion-processed"
DEFAULT_IDEMPOTENCY_TTL_HOURS = 24
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


def idempotency_ttl_hours() -> int:
    return env_int("ORCH_IDEMPOTENCY_TTL_HOURS", DEFAULT_IDEMPOTENCY_TTL_HOURS, minimum=1)


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
# LTW_NOTIFY_ON, so one list means the same thing in both deployments.
NOTIFY_KINDS = ("created", "clarification", "duplicates", "failed")


def notify_on() -> tuple[str, ...]:
    """``ORCH_NOTIFY_ON``, or all four. Unknown names are ignored, not guessed."""
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
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
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
    """A read-only, fine-grained token for the organization."""
    return os.environ.get("GITHUB_TOKEN", "").strip()


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
