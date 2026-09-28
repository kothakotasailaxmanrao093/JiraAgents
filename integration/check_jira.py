"""Read-only Jira connectivity check for the routed system.

Verifies the things the router and its children depend on, WITHOUT writing
anything: authentication, project visibility, the permissions needed to comment
and label, and whether an issue can actually be read.

    python integration/check_jira.py <project-dir> [ISSUE-KEY]

Nothing here creates, edits, comments or labels. The only write-adjacent check
is Jira's own `/mypermissions` endpoint, which reports what the account *could*
do without doing it.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def load_env(project: str) -> dict[str, str]:
    path = ROOT / project / ".env"
    if not path.exists():
        raise SystemExit(f"no .env at {path}")
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
        if m:
            env[m.group(1)] = m.group(2).strip()
    return env


def tick(ok: bool) -> str:
    return "\033[32m✔\033[0m" if ok else "\033[31m�’\033[0m".replace("’", "✘")


def main() -> int:
    project = sys.argv[1] if len(sys.argv) > 1 else "jira-orchestration"
    env = load_env(project)

    base = env.get("JIRA_BASE_URL", "").rstrip("/")
    email = env.get("JIRA_EMAIL", "")
    token = env.get("JIRA_API_TOKEN", "")
    keys = [k for k in env.get("ORCH_ALLOWED_PROJECT_KEYS", env.get("LTW_ALLOWED_PROJECT_KEYS", "")).split(",") if k]
    issue = sys.argv[2] if len(sys.argv) > 2 else ""

    print(f"\n\033[1mJira connectivity — {project}\033[0m")
    print(f"  site    : {base}")
    print(f"  account : {email}\n")

    if not (base and email and token):
        print("  missing JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN")
        return 1

    client = httpx.Client(
        base_url=base, auth=(email, token), timeout=30.0,
        headers={"Accept": "application/json"},
    )
    failures = 0

    # 1. who am I
    try:
        r = client.get("/rest/api/3/myself")
        r.raise_for_status()
        me = r.json()
        print(f"  {tick(True)} authenticated as {me.get('displayName')} <{me.get('emailAddress')}>")
        print(f"      accountId {me.get('accountId')}  ← set ORCH_BOT_ACCOUNT_ID to this")
    except Exception as e:
        print(f"  {tick(False)} authentication FAILED: {e}")
        return 1

    # 2. projects visible
    try:
        r = client.get("/rest/api/3/project/search", params={"maxResults": 50})
        r.raise_for_status()
        visible = [p["key"] for p in r.json().get("values", [])]
        print(f"  {tick(True)} {len(visible)} project(s) visible: {', '.join(visible[:10])}")
        for k in keys:
            ok = k in visible
            failures += 0 if ok else 1
            print(f"      {tick(ok)} allowed project {k} is {'visible' if ok else 'NOT VISIBLE'}")
    except Exception as e:
        print(f"  {tick(False)} project list failed: {e}")
        failures += 1

    # 3. permissions the router needs (reported, not exercised)
    wanted = ["ADD_COMMENTS", "EDIT_ISSUES", "BROWSE_PROJECTS", "CREATE_ISSUES"]
    for k in keys:
        try:
            r = client.get(
                "/rest/api/3/mypermissions",
                params={"projectKey": k, "permissions": ",".join(wanted)},
            )
            r.raise_for_status()
            perms = r.json().get("permissions", {})
            print(f"  permissions in {k}:")
            for w in wanted:
                have = bool(perms.get(w, {}).get("havePermission"))
                # CREATE_ISSUES is the child's need; the router only comments/labels.
                note = "" if have else "  ← needed" if w != "CREATE_ISSUES" else "  ← needed by task-creation"
                if not have:
                    failures += 1
                print(f"      {tick(have)} {w}{note}")
        except Exception as e:
            print(f"  {tick(False)} permission check in {k} failed: {e}")
            failures += 1

    # 4. can we actually read an issue
    if issue:
        try:
            r = client.get(f"/rest/api/3/issue/{issue}", params={"fields": "summary,labels,comment"})
            r.raise_for_status()
            f = r.json().get("fields", {})
            comments = ((f.get("comment") or {}).get("comments")) or []
            print(f"  {tick(True)} read {issue}: {f.get('summary')!r}")
            print(f"      {len(comments)} comment(s), labels={f.get('labels')}")
        except Exception as e:
            print(f"  {tick(False)} could not read {issue}: {e}")
            failures += 1
    else:
        print("  (pass an ISSUE-KEY as the 2nd argument to test reading a real issue)")

    client.close()
    print()
    print("\033[32mAll checks passed.\033[0m" if not failures
          else f"\033[31m{failures} problem(s) above.\033[0m")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
