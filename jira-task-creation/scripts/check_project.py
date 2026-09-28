"""Check that a Jira project is ready for the work-breakdown agent.

Run this once against any new project, before testing anything else. It makes
no changes — it only reads — and it tells you exactly which `.env` values to
set for that project.

Usage:
    python scripts/check_project.py ABC
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import httpx  # noqa: E402

from src.jira import api as jira  # noqa: E402

OK = "OK  "
BAD = "FAIL"
WARN = "WARN"

# Everything the agent does, and the Jira permission it needs to do it.
_PERMISSIONS = {
    "BROWSE_PROJECTS": "read the project and its tickets",
    "CREATE_ISSUES": "create the Epic, Stories and Sub-tasks",
    "EDIT_ISSUES": "link stories to the epic, and set the marker labels",
    "ADD_COMMENTS": "write the outcome comment on the trigger ticket",
}


def line(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label:<28} {detail}")


async def check(project_key: str) -> int:
    problems: list[str] = []
    advice: list[str] = []

    base_url, email, token = jira.jira_creds()
    print(f"\nChecking project '{project_key}' on {base_url or '<no JIRA_BASE_URL>'}\n")

    if not (base_url and email and token):
        line(BAD, "credentials", "set JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN in .env")
        return 1

    async with jira.jira_client(base_url, email, token) as client:
        # --- who am I -----------------------------------------------------
        try:
            resp = await client.get("/rest/api/3/myself")
            if resp.status_code != 200:
                line(BAD, "authentication", f"HTTP {resp.status_code} — check email and API token")
                return 1
            line(OK, "authentication", resp.json().get("displayName", ""))
        except httpx.HTTPError as exc:
            line(BAD, "authentication", str(exc))
            return 1

        # --- the project itself -------------------------------------------
        try:
            project = await jira.fetch_project(client, project_key)
            line(OK, "project found", f"{project.key} — {project.name} ({project.style})")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                line(BAD, "project found", f"'{project_key}' not found, or not visible to you")
                advice.append("Use the project KEY (short, uppercase), not its name.")
            else:
                line(BAD, "project found", jira._http_error_text(exc))
            return 1

        if project.description:
            line(OK, "project description", f"{project.description[:60]}...")
        else:
            line(
                WARN,
                "project description",
                "empty — the agent judges relevance against it; adding one improves results",
            )

        # --- permissions ---------------------------------------------------
        try:
            resp = await client.get(
                "/rest/api/3/mypermissions",
                params={"projectKey": project_key, "permissions": ",".join(_PERMISSIONS)},
            )
            resp.raise_for_status()
            granted = resp.json().get("permissions", {})
            for name, why in _PERMISSIONS.items():
                have = bool(granted.get(name, {}).get("havePermission"))
                line(OK if have else BAD, f"perm {name.lower()}", why)
                if not have:
                    problems.append(f"missing Jira permission {name} — needed to {why}")
        except httpx.HTTPError as exc:
            line(WARN, "permissions", f"could not read: {exc}")

        # --- issue types ---------------------------------------------------
        try:
            available = await jira.available_issue_types(client, project_key)
            line(OK, "issue types", ", ".join(available) or "none")
        except httpx.HTTPError as exc:
            line(BAD, "issue types", str(exc))
            return 1

        configured = jira.issue_type_names()
        missing = jira.missing_issue_types(list(configured.values()), available)
        if missing:
            line(BAD, "configured types", f"not in this project: {', '.join(missing)}")
            problems.append(f"issue types not available: {', '.join(missing)}")
            lowered = {name.lower(): name for name in available}
            for role, configured_name in configured.items():
                if configured_name.lower() in lowered:
                    continue
                # Suggest the closest real name for this role.
                for candidate in available:
                    if role.replace("subtask", "sub").lower() in candidate.lower().replace("-", ""):
                        advice.append(
                            f"set JIRA_{role.upper()}_ISSUE_TYPE={candidate}  "
                            f"(you have '{configured_name}')"
                        )
                        break
        else:
            line(OK, "configured types", " / ".join(configured.values()))

        # --- existing tickets ----------------------------------------------
        try:
            # Returns (issues, unavailable) — unpack it, or len() counts the
            # tuple and every project reports "2 read".
            existing, unreadable = await jira.fetch_existing_issues(client, project_key)
            line(OK, "existing tickets", f"{len(existing)} read (used for duplicate detection)")
            for problem in unreadable:
                line(WARN, "context gap", problem)
        except httpx.HTTPError as exc:
            line(WARN, "existing tickets", f"could not read: {exc}")

        # --- boards and sprints (only needed for sprint placement) ---------
        try:
            board_id, board_name = await jira.resolve_board(client, project_key)
            line(OK, "board", f"{board_id} — {board_name}")
            try:
                sprint = await jira.active_sprint(client, project_key)
                line(OK, "active sprint", f"{sprint.id} — {sprint.name}")
            except jira.SprintUnavailable as exc:
                line(WARN, "active sprint", f"{exc} (fine — backlog placement is the default)")
        except jira.SprintUnavailable as exc:
            line(WARN, "board", f"{exc} (fine unless you want sprint placement)")
        except httpx.HTTPError as exc:
            line(WARN, "board", f"could not read: {exc}")

    # --- the agent's own settings -----------------------------------------
    print()
    allowed = jira.allowed_project_keys()
    if allowed and project_key.upper() not in allowed:
        line(
            BAD, "LTW_ALLOWED_PROJECT_KEYS", f"blocks '{project_key}' (allows {', '.join(allowed)})"
        )
        problems.append(f"LTW_ALLOWED_PROJECT_KEYS does not include {project_key}")
    else:
        line(OK, "LTW_ALLOWED_PROJECT_KEYS", ", ".join(allowed) if allowed else "any project")

    if jira.read_only():
        line(WARN, "LTW_READ_ONLY", "true — the agent will refuse to create anything")
    else:
        line(OK, "LTW_READ_ONLY", "false — writes are allowed")

    line(OK, "trigger keyword", jira.trigger_keyword())
    line(OK, "marker labels", f"{jira.processed_label()} / {jira.awaiting_label()}")

    from src.notifications import email as notifier

    if notifier.is_configured():
        line(OK, "email alerts", ", ".join(notifier.recipients()))
    else:
        line(
            WARN,
            "email alerts",
            "not configured — set GMAIL_SENDER, GMAIL_APP_PASSWORD, LTW_NOTIFY_EMAILS",
        )

    # --- verdict ------------------------------------------------------------
    print()
    if problems:
        print("NOT READY. Fix these first:\n")
        for item in problems:
            print(f"  - {item}")
        for item in advice:
            print(f"  → {item}")
        print()
        return 1

    print(f"READY. Project '{project_key}' can be used with the agent.")
    if advice:
        for item in advice:
            print(f"  → {item}")
    print(
        "\nNext: create a ticket in Jira mentioning "
        f"'{jira.trigger_keyword()}', then run:\n"
        f"  python scripts/run_issue.py {project_key}-1 --dry-run\n"
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(check(sys.argv[1].strip().upper())))
