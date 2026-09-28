"""Check what `aetherion publish` is about to ship — before it ships it.

The SDK's packer copies every root file except a short ignore list, and `.env`
is not on that list. Whatever is in `.env` at the moment of publishing becomes
production's configuration, and the file itself — API token, webhook secret,
Gmail app password — travels inside the artifact.

There is no supported way to stop that from here, so the answer is to make it
impossible to do by accident: this script prints exactly what will ship, with
every secret masked, and refuses when a setting looks like it was left over
from local testing.

Usage:
    python scripts/preflight_publish.py           # check, print, exit non-zero on a problem
    python scripts/preflight_publish.py --publish # check, then run `aetherion publish`
    python scripts/preflight_publish.py --force   # check, warn, continue anyway
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

# Anything whose name contains one of these is never printed in full.
_SECRET_HINTS = ("TOKEN", "SECRET", "PASSWORD", "KEY")
# ...except these, which are identifiers rather than credentials.
_NOT_SECRET = ("JIRA_PROJECT_KEY", "LTW_ALLOWED_PROJECT_KEYS", "LTW_TRIGGER_KEYWORD")

REQUIRED = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
# Either proves a delivery really came from Jira; the server accepts both.
REQUIRED_EITHER = ("LTW_WEBHOOK_SECRET", "LTW_JIRA_WEBHOOK_SECRET")


def _is_secret(name: str) -> bool:
    if name in _NOT_SECRET:
        return False
    return any(hint in name for hint in _SECRET_HINTS)


def _mask(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 6:
        return f"<set, {len(value)} chars>"
    return f"{value[:3]}…{value[-3:]} ({len(value)} chars)"


def read_env(path: Path) -> dict[str, str]:
    """Parse a .env into a dict. Comments and blank lines are ignored."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, raw = stripped.partition("=")
        values[name.strip()] = raw.strip().strip('"').strip("'")
    return values


def show(values: dict[str, str]) -> None:
    print(f"\n.env that will be published ({len(values)} settings):\n")
    width = max((len(k) for k in values), default=0)
    for name in sorted(values):
        shown = _mask(values[name]) if _is_secret(name) else (values[name] or "<empty>")
        print(f"  {name:<{width}}  {shown}")


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def check(values: dict[str, str]) -> list[str]:
    """Return every reason this .env should not go to production."""
    problems: list[str] = []

    missing = [name for name in REQUIRED if not values.get(name, "").strip()]
    if missing:
        problems.append(f"Required setting(s) not set: {', '.join(missing)}.")

    if not any(values.get(name, "").strip() for name in REQUIRED_EITHER):
        problems.append(
            f"None of {' / '.join(REQUIRED_EITHER)} is set, so the webhook "
            "cannot tell a real Jira delivery from anyone else's POST."
        )

    if _truthy(values.get("LTW_LOCAL_EXECUTION", "")):
        problems.append(
            "LTW_LOCAL_EXECUTION is on. That makes the webhook server run the "
            "agent in its own process, which is a local development mode."
        )

    if _truthy(values.get("LTW_READ_ONLY", "")):
        problems.append(
            "LTW_READ_ONLY is on. The deployed agent will refuse every write and "
            "reply 'this deployment is in read-only mode' to every request."
        )

    if not values.get("LTW_ALLOWED_PROJECT_KEYS", "").strip():
        problems.append(
            "LTW_ALLOWED_PROJECT_KEYS is blank, so the deployed agent may create "
            "issues in EVERY project on the site. Name the projects it is for."
        )

    base = values.get("JIRA_BASE_URL", "")
    if re.search(r"(localhost|127\.0\.0\.1|ngrok|\.test\b|sandbox|demo)", base, re.IGNORECASE):
        problems.append(f"JIRA_BASE_URL looks like a test site: {base}")

    if values.get("LTW_REQUIRE_LLM", "").strip() == "":
        problems.append(
            "LTW_REQUIRE_LLM is unset (defaults to off), so a gateway outage "
            "silently produces generically-worded tickets instead of stopping. "
            "Set it explicitly, either way, so the choice is on the record."
        )

    return problems


def main() -> int:
    if not ENV_FILE.exists():
        print(f"No {ENV_FILE} — nothing would be published.", file=sys.stderr)
        return 1

    values = read_env(ENV_FILE)
    show(values)

    problems = check(values)
    forced = "--force" in sys.argv

    if problems:
        print("\nProblems:\n")
        for problem in problems:
            print(f"  ✗ {problem}")
        if not forced:
            print("\nRefusing. Fix these, or re-run with --force if they are intended.\n")
            return 1
        print("\n--force given; continuing anyway.\n")
    else:
        print("\n  ✓ Nothing looks left over from local testing.\n")

    print(
        "Remember: this file travels inside the published artifact, secrets "
        "and all. Anyone who can read the artifact can read these values.\n"
    )

    if "--publish" in sys.argv:
        print("Running: aetherion publish\n")
        return subprocess.call(["aetherion", "publish"], cwd=ROOT, env=os.environ.copy())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
