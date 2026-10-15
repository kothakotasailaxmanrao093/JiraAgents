"""The TDD, design and code commands: their rules, with no I/O (2026-10-08).

    on the Jira story      @Aetherion create TDD      readiness check, then the TDD in Confluence
    on the TDD page        @Aetherion explain why …   the Planning agent answers
                           @Aetherion why not …       … and weighs the alternative
                           @Aetherion update the TDD  the Planning agent revises the page
                           @Aetherion code            the branches are confirmed, then Coding

Pure — no network, no environment, no clock. It runs inside the workflow; the
sequences are in ``agent/command_flow.py``, the Jira and Confluence calls in
``tools/confluence_tools.py``.
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field

from .classify import strip_mention

# "source=main branch=feature/x target=main", "from main", "into release" …
_BRANCH_FIELDS = {
    "source": ("source", "from", "base", "source_branch"),
    "branch": ("branch", "name", "new", "new_branch", "working"),
    "target": ("target", "into", "to", "target_branch", "pr"),
}
_CONFIRM = ("confirm", "confirmed", "yes", "ok", "go", "go ahead", "proceed", "start")
_PER_REPO = re.compile(r"\b([\w-]+)\.(\w+)\s*(?:=|:)\s*([\w./\-]+)")


@dataclass(frozen=True)
class BranchPlan:
    """Where the work starts, the branch it is committed to, and the PR's target.

    ``per_repo`` (2026-10-15): a service whose own source or target differs —
    ``{"payments": {"source": "develop", "target": "develop"}}`` — because one
    repository works on ``main`` and another on ``develop``."""

    source: str = ""
    branch: str = ""
    target: str = ""
    per_repo: dict[str, dict[str, str]] = dataclass_field(default_factory=dict)

    def complete(self) -> bool:
        return bool(self.source and self.branch and self.target)

    def source_for(self, service: str) -> str:
        return (self.per_repo.get(service) or {}).get("source") or self.source

    def target_for(self, service: str) -> str:
        return (self.per_repo.get(service) or {}).get("target") or self.target

    def merged_over(self, defaults: BranchPlan) -> BranchPlan:
        """These choices, with ``defaults`` for any left out. A source or target
        named for every repository replaces the repositories' own defaults for it;
        one named for a single repository wins over both."""
        per_repo: dict[str, dict[str, str]] = {}
        for service, fields in defaults.per_repo.items():
            kept = {k: v for k, v in fields.items() if not getattr(self, k, "")}
            if kept:
                per_repo[service] = kept
        for service, fields in self.per_repo.items():
            per_repo[service] = {**per_repo.get(service, {}), **fields}
        return BranchPlan(
            source=self.source or defaults.source,
            branch=self.branch or defaults.branch,
            target=self.target or defaults.target,
            per_repo=per_repo,
        )

    def to_dict(self) -> dict:
        out = asdict(self)
        if not self.per_repo:
            out.pop("per_repo")
        return out

    @classmethod
    def from_dict(cls, data: dict | None) -> BranchPlan:
        data = data or {}
        per_repo = {
            str(service): {k: str(v) for k, v in (fields or {}).items() if v}
            for service, fields in (data.get("per_repo") or {}).items()
        }
        return cls(
            *(str(data.get(k) or "").strip() for k in ("source", "branch", "target")),
            per_repo={s: f for s, f in per_repo.items() if f},
        )


def parse_code_command(body: str, keyword: str = "Aetherion") -> tuple[BranchPlan, bool]:
    """(the branches named in "@Aetherion code …", whether it confirms the proposal).

    ``code source=main branch=feature/BGV-12-x target=main`` names them;
    ``code confirm`` (or yes / go ahead) accepts what was proposed.
    """
    text = strip_mention(body, keyword)
    text = re.sub(
        r"^(code|start coding|implement this|implement the tdd)\b[\s:,-]*", "", text, flags=re.I
    )
    # One repository's own branches: "payments.source=develop payments.target=develop".
    per_repo: dict[str, dict[str, str]] = {}
    for service, name, value in _PER_REPO.findall(text):
        for field_name, names in _BRANCH_FIELDS.items():
            if field_name != "branch" and name.lower() in names:
                per_repo.setdefault(service, {})[field_name] = value.strip("`'\".,")
    text = _PER_REPO.sub(" ", text)
    found: dict[str, str] = {}
    for field, names in _BRANCH_FIELDS.items():
        pattern = rf"\b(?:{'|'.join(map(re.escape, names))})\s*(?:=|:|\s)\s*([\w./\-]+)"
        match = re.search(pattern, text, flags=re.I)
        if match:
            found[field] = match.group(1).strip("`'\".,")
    lowered = text.strip().lower().rstrip(".!")
    confirmed = lowered in _CONFIRM or any(lowered.startswith(c + " ") for c in _CONFIRM)
    return BranchPlan(**found, per_repo=per_repo), confirmed


def branch_slug(issue_key: str, summary: str) -> str:
    """``feature/<KEY>-<words of the summary>`` — the default working branch."""
    words = re.sub(r"[^a-z0-9]+", "-", (summary or "").lower()).strip("-")[:40].strip("-")
    return f"feature/{issue_key.upper()}" + (f"-{words}" if words else "")


def branch_question(
    plan: BranchPlan,
    repo: str,
    keyword: str = "Aetherion",
    repos: dict[str, str] | None = None,
) -> list[tuple[str, object]]:
    """The reply asking the person to confirm, or change, the branches — one line per
    repository when they differ (2026-10-15)."""
    reply_with = [
        f'"@{keyword} code confirm" — use these',
        f'"@{keyword} code source={plan.source} branch={plan.branch} '
        f'target={plan.target}" — with your own names',
    ]
    if plan.per_repo or len(repos or {}) > 1:
        proposed = [f"New working branch, in every repository: {plan.branch}"] + [
            f"{service} ({slug}): from {plan.source_for(service)} → pull request into "
            f"{plan.target_for(service)}"
            for service, slug in (repos or {}).items()
        ]
        example = next(iter(plan.per_repo or repos or {}), "service")
        reply_with.append(
            f'"@{keyword} code confirm {example}.source=develop {example}.target=develop" '
            "— one repository's own branches"
        )
    else:
        proposed = [
            f"Repository: {repo}" if repo else "Repository: from the design",
            f"Start from (source): {plan.source}",
            f"New working branch: {plan.branch}",
            f"Pull request into (target): {plan.target}",
        ]
    return [
        ("Confirm the branches before coding starts", ""),
        ("Proposed", proposed),
        ("Reply with one of", reply_with),
    ]


# --- Confluence: replies and the TDD page ----------------------------------------------

_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)|(https?://[^\s<>()\]]+)")


def _inline(text: str) -> str:
    """Escaped text with "[label](url)" and bare URLs as links."""
    out, pos = [], 0
    for m in _LINK.finditer(text):
        out.append(html.escape(text[pos : m.start()]))
        label, url = (m.group(1), m.group(2)) if m.group(2) else (m.group(3), m.group(3))
        out.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def blocks_to_storage(blocks: list[tuple[str, object]]) -> str:
    """The reply's ``(heading, body)`` blocks as Confluence storage format — the
    same blocks a Jira reply is built from, so both read alike."""
    parts: list[str] = []
    for head, body in blocks:
        if isinstance(body, list):
            items = [str(i) for i in body if str(i).strip()]
            if head:
                parts.append(f"<h4>{_inline(str(head))}</h4>")
            if items:
                parts.append("<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>")
            continue
        text = str(body or "").strip()
        if head and not text:
            parts.append(f"<p><strong>{_inline(str(head))}</strong></p>")
        elif text and not head:
            parts.append(
                "".join(f"<p>{_inline(line)}</p>" for line in text.splitlines() if line.strip())
            )
        elif text:
            parts.append(f"<h4>{_inline(str(head))}</h4>")
            parts.append(
                "".join(f"<p>{_inline(line)}</p>" for line in text.splitlines() if line.strip())
            )
    return "".join(parts) or "<p>(no content)</p>"


def tdd_version(page_version: int) -> str:
    """The TDD's own version from the page's: the first page is v1.0, each revision
    adds one — v1.1, v1.2 … (2026-10-08). Confluence keeps every earlier one."""
    return f"v1.{max(0, int(page_version) - 1)}"


def tdd_title(issue_key: str, page_version: int = 1) -> str:
    """One page per story: "BGV-105 - TDD v1.0", renamed as it is revised."""
    return f"{issue_key.upper()} - TDD {tdd_version(page_version)}"


def tdd_page_header(
    issue_key: str,
    story_url: str,
    keyword: str = "Aetherion",
    version: str = "v1.0",
    summary: str = "",
    related: list[dict[str, str]] | None = None,
) -> str:
    """The note at the top of every TDD page: where it comes from, how to discuss
    it, and the project's earlier designs it builds on (2026-10-15)."""
    at = f"@{html.escape(keyword)}"
    about = f" — {html.escape(summary)}" if summary else ""
    lines = [
        f"<strong>TDD {html.escape(version)}</strong> for "
        f'<a href="{html.escape(story_url, quote=True)}">{html.escape(issue_key)}</a>{about}, '
        "written by the Planning agent. Earlier versions are in the page history.",
        f"Discuss it in the comments below — <strong>{at} explain why …</strong>, "
        f"<strong>{at} why not …</strong>; change it with "
        f"<strong>{at} update the TDD</strong>; implement it with "
        f"<strong>{at} code</strong>.",
    ]
    links = [
        f'<a href="{html.escape(r["url"], quote=True)}">{html.escape(r["title"])}</a>'
        for r in related or []
        if r.get("url") and r.get("title")
    ]
    if links:
        lines.append(
            "Builds on the earlier designs in this project (their decisions are kept): "
            + ", ".join(links)
            + "."
        )
    body = "".join(f"<p>{line}</p>" for line in lines)
    return (
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
        f"{body}</ac:rich-text-body></ac:structured-macro>"
    )
