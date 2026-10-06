"""GitHub pull requests as input for the Planning agent (2026-10-03).

Every minute the router asks GitHub what changed in the organization's repos
(``tools/github_tools.py``). Two things are passed on, each once:

* a pull request **merged into the main branch**, with its changes and its
  whole conversation;
* **new comments** on a pull request that is not merged yet.

The Planning agent takes one input, ``issue_text``, so each event is written
out as plain text. Pure: no network, no clock, no environment.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

# GitHub's "updated" times and our own clock can disagree by seconds, and a
# merge can land while a check is running; every check re-reads this much of
# the previous one, and what was already sent is skipped by its key.
OVERLAP = timedelta(minutes=2)
# How long a sent key is remembered: only the overlap is ever re-read.
REMEMBER = timedelta(days=1)

MAX_DESCRIPTION = 4000
MAX_COMMENT = 600
MAX_COMMENTS = 30
MAX_FILES = 100
# The whole text, so a waiting event fits one Jira property (32 KB) with room.
MAX_TEXT = 20000

_KEY_LIKE = re.compile(r"\b([A-Za-z][A-Za-z0-9]+)-(\d+)\b")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class Comment:
    id: str  # "issue-123", "review-45", "line-67": GitHub keeps three kinds
    author: str
    body: str
    created: str
    file: str = ""
    line: int | None = None

    def as_line(self) -> str:
        where = (
            f" on {self.file}" + (f" line {self.line}" if self.line else "") if self.file else ""
        )
        body = " ".join(self.body.split())
        if len(body) > MAX_COMMENT:
            body = body[: MAX_COMMENT - 1] + "…"
        return f"- {self.author}{where}: {body}"


@dataclass
class Event:
    kind: str  # "pr_merged" | "pr_comments"
    key: str
    repo: str  # "org/name"
    pr_number: int
    pr_url: str
    issue_text: str
    issue_key: str = ""  # a Jira key named in the PR's title or branch
    marks: list[str] = field(default_factory=list)  # keys recorded once it is sent

    def as_dict(self) -> dict:
        return asdict(self)


def is_bot(user: dict | None) -> bool:
    login = str((user or {}).get("login") or "")
    return (user or {}).get("type") == "Bot" or login.endswith("[bot]")


def comments_from(
    issue_comments: list[dict], line_comments: list[dict], reviews: list[dict]
) -> list[Comment]:
    """GitHub's three kinds of PR comment as one list, oldest first, bots left out."""
    out: list[Comment] = []
    for c in issue_comments:
        if not is_bot(c.get("user")) and (c.get("body") or "").strip():
            out.append(Comment(f"issue-{c['id']}", _login(c), c["body"], c.get("created_at") or ""))
    for c in line_comments:
        if not is_bot(c.get("user")) and (c.get("body") or "").strip():
            out.append(
                Comment(
                    f"line-{c['id']}",
                    _login(c),
                    c["body"],
                    c.get("created_at") or "",
                    file=c.get("path") or "",
                    line=c.get("line") or c.get("original_line"),
                )
            )
    for r in reviews:
        if not is_bot(r.get("user")) and (r.get("body") or "").strip():
            out.append(
                Comment(f"review-{r['id']}", _login(r), r["body"], r.get("submitted_at") or "")
            )
    return sorted(out, key=lambda c: (c.created, c.id))


def _login(item: dict) -> str:
    return str((item.get("user") or {}).get("login") or "someone")


def merged_into(pr: dict, branch: str, since: datetime) -> bool:
    """Merged into ``branch`` at or after ``since`` (closed without merging is not)."""
    merged = parse_time(pr.get("merged_at"))
    return bool(merged and merged >= since and (pr.get("base") or {}).get("ref") == branch)


def jira_key(pr: dict, projects: tuple[str, ...] = ()) -> str:
    """The Jira key a PR names in its title or branch ("BGV-93", "feature/bgv-93-x").

    Only keys of ``projects`` count, in any case; with none given, only keys
    written in capitals — "feature/change-4" is not ticket CHANGE-4.
    """
    for text in (pr.get("title") or "", (pr.get("head") or {}).get("ref") or ""):
        for prefix, number in _KEY_LIKE.findall(text):
            if (prefix.upper() in projects) if projects else prefix.isupper():
                return f"{prefix.upper()}-{number}"
    return ""


def merge_key(repo: str, pr: dict) -> str:
    return f"merged:{repo}#{pr['number']}:{pr.get('merge_commit_sha') or ''}"


def comment_key(repo: str, number: int, comment: Comment) -> str:
    return f"comment:{repo}#{number}:{comment.id}"


def merged_event(
    repo: str, pr: dict, files: list[dict], comments: list[Comment], projects: tuple[str, ...] = ()
) -> Event:
    marks = [merge_key(repo, pr)] + [comment_key(repo, pr["number"], c) for c in comments]
    return Event(
        kind="pr_merged",
        key=marks[0],
        repo=repo,
        pr_number=pr["number"],
        pr_url=pr.get("html_url") or "",
        issue_text=merged_text(repo, pr, files, comments),
        issue_key=jira_key(pr, projects),
        marks=marks,
    )


def comments_event(
    repo: str, pr: dict, comments: list[Comment], projects: tuple[str, ...] = ()
) -> Event:
    marks = [comment_key(repo, pr["number"], c) for c in comments]
    return Event(
        kind="pr_comments",
        key=f"comments:{repo}#{pr['number']}:{comments[0].id}..{comments[-1].id}",
        repo=repo,
        pr_number=pr["number"],
        pr_url=pr.get("html_url") or "",
        issue_text=comments_text(repo, pr, comments),
        issue_key=jira_key(pr, projects),
        marks=marks,
    )


def _header(pr: dict) -> list[str]:
    head = (pr.get("head") or {}).get("ref") or "?"
    base = (pr.get("base") or {}).get("ref") or "?"
    body = (pr.get("body") or "").strip() or "(none)"
    if len(body) > MAX_DESCRIPTION:
        body = body[: MAX_DESCRIPTION - 1] + "…"
    return [
        f"PR #{pr['number']}: {pr.get('title') or '(no title)'}",
        f"Link: {pr.get('html_url') or ''}",
        f"Author: {_login(pr)} · Branch: {head} → {base}",
        "",
        "Description:",
        body,
    ]


def _comment_lines(title: str, comments: list[Comment]) -> list[str]:
    if not comments:
        return [f"{title}: none"]
    shown = comments[-MAX_COMMENTS:]
    lines = [f"{title} ({len(comments)}):"] + [c.as_line() for c in shown]
    if len(comments) > len(shown):
        lines.insert(1, f"(the {len(comments) - len(shown)} earliest are not shown)")
    return lines


def merged_text(repo: str, pr: dict, files: list[dict], comments: list[Comment]) -> str:
    sha = (pr.get("merge_commit_sha") or "")[:7]
    base = (pr.get("base") or {}).get("ref") or "main"
    file_lines = [_file_line(f) for f in files[:MAX_FILES]]
    if len(files) > MAX_FILES:
        file_lines.append(f"- … and {len(files) - MAX_FILES} more")
    return _capped(
        "\n".join(
            [f"A pull request was merged into {base} in {repo} (merge commit {sha}).", ""]
            + _header(pr)
            + ["", f"Changed files ({len(files)}):"]
            + (file_lines or ["- none"])
            + [""]
            + _comment_lines("Comments", comments)
        )
    )


def _file_line(f: dict) -> str:
    changes = f"+{f.get('additions', 0)} −{f.get('deletions', 0)}"
    return f"- {f.get('filename')} ({f.get('status')}, {changes})"


def _capped(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return text[: MAX_TEXT - 40].rstrip() + "\n… (shortened to fit)"


def comments_text(repo: str, pr: dict, comments: list[Comment]) -> str:
    if pr.get("merged_at"):
        state = "already merged"
    else:
        state = "open" if pr.get("state") == "open" else "closed, not merged"
    return _capped(
        "\n".join(
            [f"New comments on a pull request ({state}) in {repo}.", ""]
            + _header(pr)
            + [""]
            + _comment_lines("New comments", comments)
        )
    )


def prune(sent: dict[str, str], now: datetime) -> dict[str, str]:
    """Sent keys still inside the window a check can re-read."""
    keep: dict[str, str] = {}
    for key, at in sent.items():
        when = parse_time(at)
        if when and now - when <= REMEMBER:
            keep[key] = at
    return keep
