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
