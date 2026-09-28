"""Delete Jira issues, children before parents.

A cleanup tool for tickets produced by a bad run. It is deliberately explicit:
it prints what it will delete and asks for confirmation, because deleting a
Jira issue cannot be undone.

Usage:
    python scripts/delete_issues.py TT2-138..TT2-160
    python scripts/delete_issues.py TT2-138 TT2-139 TT2-140
    python scripts/delete_issues.py TT2-138..TT2-160 --yes   # skip the prompt
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from src.jira import api as jira  # noqa: E402

# Children must go first: Jira refuses to delete a parent that still has them.
_ORDER = ("Sub-task", "Subtask", "Story", "Task", "Bug", "Epic")


def expand(args: list[str]) -> list[str]:
    """Turn 'TT2-138..TT2-160' into every key in that range."""
    keys: list[str] = []
    for arg in args:
        if ".." in arg:
            start, _, end = arg.partition("..")
            prefix, _, first = start.rpartition("-")
            _, _, last = end.rpartition("-")
            keys += [f"{prefix}-{n}" for n in range(int(first), int(last) + 1)]
        else:
            keys.append(arg.strip().upper())
    return keys


async def main(keys: list[str], assume_yes: bool) -> int:
    base_url, email, token = jira.jira_creds()
    if not (base_url and email and token):
        print("Missing Jira credentials in .env")
        return 1

    async with jira.jira_client(base_url, email, token) as client:
        found: dict[str, list[str]] = {}
        missing: list[str] = []
        for key in keys:
            try:
                fields = (await jira.fetch_issue(client, key))["fields"]
                kind = fields["issuetype"]["name"]
                found.setdefault(kind, []).append(f"{key}|{fields.get('summary', '')[:60]}")
            except Exception:
                missing.append(key)

        total = sum(len(v) for v in found.values())
        if not total:
            print("Nothing to delete — none of those issues exist.")
            return 0

        print(f"\nAbout to PERMANENTLY DELETE {total} issue(s):\n")
        for kind in sorted(found, key=lambda k: _ORDER.index(k) if k in _ORDER else 99):
            for entry in found[kind]:
                key, _, summary = entry.partition("|")
                print(f"  {key:10} [{kind:9}] {summary}")
        if missing:
            print(f"\n  (skipping {len(missing)} that no longer exist)")

        if not assume_yes:
            print("\nThis cannot be undone.")
            if input("Type 'delete' to confirm: ").strip().lower() != "delete":
                print("Cancelled. Nothing was deleted.")
                return 1

        deleted, failed = [], []
        for kind in sorted(found, key=lambda k: _ORDER.index(k) if k in _ORDER else 99):
            for entry in found[kind]:
                key = entry.split("|", 1)[0]
                resp = await client.delete(f"/rest/api/3/issue/{key}")
                if resp.status_code in (200, 204):
                    deleted.append(key)
                    print(f"  deleted {key}")
                else:
                    failed.append(f"{key}: HTTP {resp.status_code} {resp.text[:120]}")

        print(f"\nDeleted {len(deleted)} issue(s).")
        for problem in failed:
            print(f"  FAILED {problem}")
        return 1 if failed else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--yes"]
    if not args:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(expand(args), "--yes" in sys.argv)))
