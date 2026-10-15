"""What follows a build: Review -> Planning -> Coding (2026-10-08).

The flow the team drew: a person asks "@Aetherion build"; Task Creation creates
the tickets; the orchestrator has Requirement Review look for gaps; Planning
writes a technical design (TDD) from the tickets, the review and the code graph;
Coding implements that design and raises a pull request — never merged, a person
approves it in GitHub. When Coding cannot implement the design, its reasons go
back to Planning for a revised one, and Coding tries again.

Which of those steps follow a build is a setting (``ORCH_BUILD_PIPELINE``);
with none set, a build ends where it always did.

Pure — no I/O, no environment, no clock. It runs inside the workflow; the
sequence that calls the agents is ``agent/pipeline_flow.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from shared.contract import AgentResult, Outcome

# A build these outcomes ended with has tickets (or a plan of them) to go on.
CONTINUES = (Outcome.CREATED, Outcome.ALREADY_EXISTS, Outcome.PLANNED)

# What a person reads for each step, in the reply's checklist.
STEP_NAMES = {
    "build": "Work breakdown",
    "review": "Review",
    "plan": "Technical design",
    "code": "Pull request",
}

MAX_TICKETS = 40  # tickets described to Planning; the rest are named only
MAX_DESCRIPTION = 3000  # characters of each ticket's description
MAX_POINTS = 12  # review points handed on


@dataclass(frozen=True)
class PipelineConfig:
    """The steps after a build, and where their agents run (from settings)."""

    steps: tuple[str, ...] = ()
    repos: str = ""
    planning_agent: str = "planning_agent"
    planning_queue: str = ""
    planning_timeout_minutes: int = 15
    coding_agent: str = "coding-agent"
    coding_queue: str = ""
    coding_timeout_minutes: int = 20
    coding_attempts: int = 2
    plan_min_readiness: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PipelineConfig:
        data = data or {}
        known = cls.__dataclass_fields__
        values = {k: v for k, v in data.items() if k in known and v is not None}
        values["steps"] = tuple(str(s) for s in values.get("steps") or ())
        return cls(**values)

    def follows(self, build: AgentResult | None) -> bool:
        """Does anything run after this build?"""
        return bool(self.steps) and build is not None and build.outcome in CONTINUES

    def budget_minutes(self, build_minutes: int, review_minutes: int) -> int:
        """How long the whole job may hold the ticket, so it is not taken for dead."""
        total = build_minutes
        if "review" in self.steps:
            total += review_minutes
        if "plan" in self.steps:
            total += self.planning_timeout_minutes
        if "code" in self.steps:
            # Each Coding attempt after the first is preceded by a new design.
            total += self.coding_attempts * self.coding_timeout_minutes
            total += (self.coding_attempts - 1) * self.planning_timeout_minutes
        return total


@dataclass
class Step:
    """One step after the build, as the reply reports it."""

    name: str  # review | plan | code
    status: str = "waiting"  # waiting | running | done | failed | skipped
    detail: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return STEP_NAMES.get(self.name, self.name)


MARK = {"waiting": "◻", "running": "⏳", "done": "✅", "failed": "❌", "skipped": "➖"}


def ticket_keys(build: AgentResult) -> list[str]:
    """The tickets the build created, or found already existing."""
    keys = [c.key for c in build.created if c.key]
    keys += [d.existing_key for d in build.duplicates if d.existing_key]
    return list(dict.fromkeys(keys))


def review_points(review: AgentResult | None) -> list[str]:
    """What the review left open: its questions, else its findings."""
    if review is None or review.outcome is not Outcome.REVIEWED:
        return []
    points = [q for q in review.questions if str(q).strip()]
    if not points:
        for finding in review.findings:
            text = str(finding.get("description") or finding.get("title") or "").strip()
            if text:
                points.append(text)
    return points[:MAX_POINTS]


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def planning_text(
    root: dict[str, Any],
    tickets: list[dict[str, Any]],
    proposed: list[str],
    review: dict[str, Any] | None,
) -> str:
    """Planning's ``issue_text``: the request, the tickets and what the review found.

    ``root`` and each ticket are ``{"key", "type", "summary", "description"}`` as
    read from Jira; ``proposed`` is the breakdown when nothing was created (a
    PDF-only build).
    """
    lines = [
        f"Requirement {root.get('key', '')}: {root.get('summary', '')}".strip(),
        "",
        _clip(str(root.get("description") or ""), MAX_DESCRIPTION) or "(no description)",
    ]
    if tickets:
        lines += ["", "Work items created in Jira for it:"]
        for ticket in tickets[:MAX_TICKETS]:
            name = f"{ticket.get('type') or ''} {ticket.get('key') or ''}".strip()
            lines.append(f"- {name}: {ticket.get('summary') or ''}")
            description = _clip(str(ticket.get("description") or ""), MAX_DESCRIPTION)
            if description:
                lines += [f"  {line}" for line in description.splitlines() if line.strip()]
        rest = [str(t.get("key", "")) for t in tickets[MAX_TICKETS:]]
        if rest:
            lines.append(f"- and {len(rest)} more: {', '.join(rest)}")
    elif proposed:
        lines += ["", "Work items proposed for it (not created in Jira):"]
        lines += [f"- {item}" for item in proposed]

    result = AgentResult.from_dict(review) if review else None
    points = review_points(result)
    if result is not None and result.outcome is Outcome.REVIEWED:
        score = f" — readiness {result.readiness_score}/5" if result.readiness_score else ""
        lines += ["", f"Requirement review{score}. Open points to design for or call out:"]
        lines += [f"- {p}" for p in points] or ["- none"]
    return "\n".join(lines).strip()


def replan_text(issue_text: str, coding: dict[str, Any]) -> str:
    """The same request, with why the last design could not be implemented."""
    reasons = coding_problems(coding)
    return (
        issue_text
        + "\n\nA previous technical design for this could not be implemented automatically:\n"
        + "\n".join(f"- {r}" for r in reasons)
        + "\nRevise the design so every change names a real file in the named service,"
        " and address the points above."
    )


def tdd_key(planning: dict[str, Any] | None) -> str:
    """The stored TDD PDF's key from Planning's result, or ""."""
    for item in (planning or {}).get("results") or []:
        if isinstance(item, dict) and item.get("file_key"):
            return str(item["file_key"])
    return ""


def planning_problem(planning: dict[str, Any] | None) -> str:
    planning = planning or {}
    return str(planning.get("message") or planning.get("error") or "Planning returned no design")


def pull_requests(coding: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [pr for pr in (coding or {}).get("pull_requests") or [] if pr.get("pr_url")]


# GitHub refusing the token, or not reachable: a new design cannot fix these.
_ACCESSMARKERS = (
    "401",
    "403",
    "Bad credentials",
    "Resource not accessible",
    "RepoFetchError",
    "GitHubApiError",
)
ACCESS_HINT = (
    "GitHub refused the write. The token Coding uses (GITHUB_TOKEN) needs Contents and "
    "Pull requests: read and write, on this repository — then ask again."
)


def _short(reason: str) -> str:
    """One readable line: no URL, no help link, GitHub's own message kept."""
    first = re.sub(
        r" for url '[^']+'", "", (reason or "").strip().splitlines()[0] if reason else ""
    )
    message = re.search(r'"message"\s*:\s*"([^"]+)"', reason or "")
    if message and message.group(1) not in first:
        first += f" — {message.group(1)}"
    return first[:300]


def coding_problems(coding: dict[str, Any] | None) -> list[str]:
    """Why Coding raised no pull request: one line per distinct reason."""
    coding = coding or {}
    files_by_reason: dict[tuple[str, str], list[str]] = {}
    for item in coding.get("skipped") or []:
        if isinstance(item, dict):
            key = (str(item.get("service") or ""), _short(str(item.get("reason") or "")))
            files_by_reason.setdefault(key, []).append(str(item.get("file_path") or ""))
    reasons = []
    for (service, reason), files in files_by_reason.items():
        where = files[0] if len(files) == 1 else f"{len(files)} files"
        reasons.append(f"{service} ({where}): {reason}" if service else reason)
    if not reasons:
        reasons = [
            _short(str(coding.get("message") or coding.get("error") or ""))
            or "no pull request raised"
        ]
    return reasons[:MAX_POINTS]


def tests_failed(coding: dict[str, Any] | None) -> bool:
    """A pull request was raised, and its own tests ran and failed — Coding did not
    finish the job, so (when attempts remain) it goes back to Planning (2026-10-08)."""
    prs = pull_requests(coding)
    return bool(prs) and all(pr.get("tests_ran") and not pr.get("tests_passed") for pr in prs)


def test_failures(coding: dict[str, Any] | None, limit: int = 8) -> list[str]:
    """The errors the failing tests printed — what a revised design must address."""
    lines: list[str] = []
    for pr in pull_requests(coding):
        for line in str(pr.get("verification_detail") or "").splitlines():
            line = re.sub(r"/\S*/repo_fetcher_\w+/", "", line).strip()
            if ("ERROR" in line or "FAIL" in line or "error:" in line) and line not in lines:
                if (
                    "Help 1" in line
                    or "Re-run Maven" in line
                    or line in ("[ERROR]", "[ERROR] -> [Help 1]")
                ):
                    continue
                lines.append(line[:300])
    return lines[:limit] or ["the pull request's tests failed"]


def is_design_problem(coding: dict[str, Any] | None) -> bool:
    """True when a new design could help: Coding ran and named changes it could not make.

    A run that failed before that — a timeout, a crash, an unreadable TDD — and a
    refused token are not the design's fault; sending them back to Planning only
    repeats the work (BGV-105, 2026-10-08: a timeout was "revised" twice).
    """
    coding = coding or {}
    if coding.get("status") != "no_pull_requests" or not coding.get("skipped"):
        return False
    return not is_access_problem(coding)


def is_access_problem(coding: dict[str, Any] | None) -> bool:
    """True when every reason is GitHub refusing or unreachable — not the design."""
    reasons = coding_problems(coding)
    return all(any(m in r for m in _ACCESSMARKERS) for r in reasons)


def pr_lines(coding: dict[str, Any] | None) -> list[str]:
    lines = []
    for pr in pull_requests(coding):
        if pr.get("tests_ran"):
            # The repository's own CI, when it gave the verdict (2026-10-15).
            who = "CI" if pr.get("verified_by") == "ci" else "tests"
            tests = f"{who} passed" if pr.get("tests_passed") else f"{who} FAILED — see the PR"
        else:
            # Coding opens it as a draft (2026-10-14): nobody has proven it works.
            tests = "tests not run — a draft; run the tests before merging"
        lines.append(f"{pr.get('service', '')}: {pr['pr_url']} ({tests})")
    return lines


def rollout_lines(coding: dict[str, Any] | None) -> list[str]:
    """The order to merge and deploy several services' PRs, with each step's reason
    (2026-10-15); [] for one PR. Coding returns its PRs in that order."""
    prs = pull_requests(coding)
    if len(prs) < 2:
        return []
    reasons = {}
    for step in (coding or {}).get("rollout_order") or []:
        service, _, why = str(step).partition(":")
        reasons[service.strip().lower().lstrip("0123456789. ")] = why.strip()
    lines = []
    for i, pr in enumerate(prs, start=1):
        service = str(pr.get("service", ""))
        why = reasons.get(service.lower(), "")
        lines.append(f"{i}. {service}: {pr['pr_url']}" + (f" — {why}" if why else ""))
    return lines


def add_sections(
    blocks: list[tuple[str, object]], steps: list[Step], ran: list[str]
) -> list[tuple[str, object]]:
    """The build's reply, with each step after it added before the footer.

    Each step's heading carries its state (✅ ⏳ ❌ ➖ ◻), so the same function
    draws the reply while the chain runs and when it ends. "Handled by" is
    rewritten to name every agent that ran, in order.
    """
    footer = blocks[-1:] if blocks and blocks[-1][0] == "" else []
    body = blocks[: len(blocks) - len(footer)]
    out: list[tuple[str, object]] = []
    for heading, value in body:
        if heading == "Handled by" and ran:
            value = "Jira Orchestration → " + ", then ".join(ran)
        out.append((heading, value))
    for step in steps:
        content = ([step.detail] if step.detail else []) + step.lines
        out.append((f"{MARK.get(step.status, '◻')} {step.label}", content or ["waiting"]))
    return out + footer


def summary(steps: list[Step]) -> list[dict[str, Any]]:
    """The steps as data, for the run's own result (what the local form shows)."""
    return [
        {"step": s.name, "status": s.status, "detail": s.detail, "lines": list(s.lines)}
        for s in steps
    ]
