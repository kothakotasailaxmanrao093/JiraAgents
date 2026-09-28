"""Publish one agent, with every step that has ever caused an incident enforced.

Run it from inside the agent you want to publish:

    cd jira-task-creation
    uv run python scripts/publish.py --bump patch

It refuses to publish unless, in order:

  1. the shared package is mirrored and in sync
  2. the tests pass
  3. ruff and black pass
  4. ``pyproject.toml`` and ``src/agent/metadata.json`` carry the SAME version
  5. the version was raised in this run (``--bump``) or explicitly kept
     (``--no-bump``), because publishing over an existing version leaves the old
     build running while reporting success
  6. the agent's own preflight passes, where it has one (the work-breakdown
     agent checks what its ``.env`` would ship, since ``.env`` travels inside the
     artifact)

Only then does it run ``uv sync`` followed by ``aetherion publish``, and it prints the liveness check to
run afterwards — an upload returning 202 does not mean the worker has swapped.

This is the implementation; each agent has a one-line ``scripts/publish.py``
shim so you never have to remember where it lives.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Agents this script knows how to publish. A fourth is one entry.
AGENTS = {
    "jira-task-creation": {
        "metadata": "src/agent/metadata.json",
        # The version ALSO lives as a constant, because a workflow may not read
        # files. All three are written together.
        "version_constant": "src/agent/delegated.py",
        "preflight": "scripts/preflight_publish.py",
        "live_marker": "mode :",
        "live_file": "src/agent/agent.py",
    },
    "jira-requirement-review": {
        "metadata": "src/agent/metadata.json",
        "version_constant": "src/config.py",
        "preflight": None,
        "live_marker": None,
        "live_file": None,
    },
    "jira-orchestration": {
        "metadata": "src/agent/metadata.json",
        "version_constant": "src/agent/router_agent.py",
        "preflight": None,
        "live_marker": "mode : webhook",
        "live_file": "src/agent/router_agent.py",
    },
}

_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


class Failed(Exception):
    """A step that must stop the publish."""


def _step(number: int, title: str) -> None:
    print(f"\n\033[1m[{number}/7] {title}\033[0m")


def _run(command: list[str], cwd: Path, *, what: str) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-4000:])
        print(result.stderr[-4000:], file=sys.stderr)
        raise Failed(what)
    return result.stdout


def detect_agent(start: Path) -> tuple[str, Path]:
    """Which agent are we standing in?"""
    for candidate in (start, *start.parents):
        if candidate.name in AGENTS and (candidate / "pyproject.toml").exists():
            return candidate.name, candidate
    raise Failed(
        f"Run this from inside an agent directory ({', '.join(AGENTS)}), not {start}."
    )


# --------------------------------------------------------------------------
# Versions — the step people skip, and the one that matters most
# --------------------------------------------------------------------------


def read_pyproject_version(project: Path) -> str:
    match = _VERSION_RE.search((project / "pyproject.toml").read_text())
    if not match:
        raise Failed("no version found in pyproject.toml")
    return match.group(1)


def read_metadata_version(project: Path, rel: str) -> str:
    return str(json.loads((project / rel).read_text()).get("version", ""))


def bump(version: str, part: str) -> str:
    bits = version.split(".")
    while len(bits) < 3:
        bits.append("0")
    try:
        major, minor, patch = (int(b) for b in bits[:3])
    except ValueError as exc:
        raise Failed(f"version {version!r} is not numeric; bump it by hand") from exc
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


_CONSTANT_RE = re.compile(r'^AGENT_VERSION\s*=\s*"([^"]+)"', re.MULTILINE)


def read_constant_version(project: Path, rel: str | None) -> str | None:
    if not rel:
        return None
    match = _CONSTANT_RE.search((project / rel).read_text())
    return match.group(1) if match else None


def write_versions(project: Path, rel_metadata: str, new: str, rel_constant: str | None) -> None:
    """Raise the version in ALL THREE places, together.

    pyproject and metadata.json drifted once and the UI reported a version
    nobody had deployed. The third — the AGENT_VERSION constant — exists because
    a Temporal workflow may not read files, so the agent cannot look its own
    version up at runtime."""
    pyproject = project / "pyproject.toml"
    pyproject.write_text(_VERSION_RE.sub(f'version = "{new}"', pyproject.read_text(), count=1))

    meta_path = project / rel_metadata
    raw = meta_path.read_text()
    data = json.loads(raw)
    data["version"] = new
    indent = 4 if raw.startswith("{\n    ") else 2
    meta_path.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + "\n")

    if rel_constant:
        const_path = project / rel_constant
        const_path.write_text(
            _CONSTANT_RE.sub(f'AGENT_VERSION = "{new}"', const_path.read_text(), count=1)
        )


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish an Aetherion agent, safely.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--bump",
        choices=("patch", "minor", "major"),
        help="raise the version in pyproject.toml AND metadata.json before publishing",
    )
    group.add_argument(
        "--no-bump",
        action="store_true",
        help="publish the current version (only safe if you already raised it)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="run every check, then stop before uploading"
    )
    parser.add_argument(
        "--force-preflight",
        action="store_true",
        help=(
            "pass --force to the agent's preflight, accepting its warnings. Use ONLY "
            "when you have read each warning and confirmed it is intended — e.g. a "
            "real Jira site whose name merely looks like a test one."
        ),
    )
    args = parser.parse_args()

    try:
        name, project = detect_agent(Path.cwd().resolve())
        config = AGENTS[name]
        print(f"\033[1mPublishing {name}\033[0m  ({project})")

        _step(1, "Mirroring the shared package")
        sync = REPO / "scripts" / "sync_shared.py"
        if not sync.exists():
            raise Failed(f"{sync} is missing — cannot verify the shared package")
        print(_run([sys.executable, str(sync)], REPO, what="shared sync failed").strip())

        _step(2, "Tests")
        _run(["uv", "run", "pytest", "-q", "-p", "no:warnings"], project, what="tests failed")
        print("  tests pass")

        _step(3, "Lint and formatting")
        _run(["uv", "run", "ruff", "check", "src", "tests"], project, what="ruff failed")
        _run(["uv", "run", "black", "--check", "src", "tests"], project, what="black failed")
        print("  ruff and black pass")

        _step(4, "Version agreement")
        current = read_pyproject_version(project)
        meta = read_metadata_version(project, config["metadata"])
        if current != meta:
            raise Failed(
                f"pyproject.toml says {current} but {config['metadata']} says {meta}. "
                "They must match — the platform registers the metadata version."
            )
        constant = read_constant_version(project, config.get("version_constant"))
        if constant is not None and constant != current:
            raise Failed(
                f"pyproject.toml says {current} but AGENT_VERSION in "
                f"{config['version_constant']} says {constant}. The agent would report "
                "a version it is not."
            )
        places = 3 if constant is not None else 2
        print(f"  all {places} places say {current}")

        _step(5, "Version bump")
        if args.bump:
            new = bump(current, args.bump)
            write_versions(project, config["metadata"], new, config.get("version_constant"))
            print(f"  {current} -> {new}  (pyproject.toml and {config['metadata']})")
            current = new
        else:
            print(f"  keeping {current} — publishing over an existing version may")
            print("  leave the OLD build running while reporting success.")

        _step(6, "Agent preflight")
        preflight = config["preflight"]
        if preflight and (project / preflight).exists():
            command = [sys.executable, preflight]
            if args.force_preflight:
                # Deliberately loud: this overrides a guard that exists because
                # .env ships inside the artifact.
                print("  --force-preflight: accepting the warnings below as intended")
                command.append("--force")
            print(_run(command, project, what="preflight failed"))
        else:
            print("  (this agent has no preflight script)")

        _step(7, "Publish")
        # After the bump, so uv.lock and the environment carry the new version
        # and any dependency change before the artifact is built.
        _run(["uv", "sync"], project, what="uv sync failed")
        print("  uv sync done")
        if args.dry_run:
            print(f"  --dry-run: stopping before upload. Would publish {name} {current}.")
            return 0
        print(_run(["uv", "run", "aetherion", "publish"], project, what="publish failed"))

    except Failed as exc:
        print(f"\n\033[31mSTOPPED: {exc}\033[0m", file=sys.stderr)
        print("Nothing was published.", file=sys.stderr)
        return 1

    print(f"\n\033[32mUploaded {name} {current}.\033[0m")
    print("\n  202 Accepted means the upload was received — NOT that the worker restarted.")
    if config["live_marker"]:
        print(
            f"  Confirm it is live: comment on a ticket, then check the logs show the\n"
            f"  line number that `grep -n \"{config['live_marker']}\" {config['live_file']}`\n"
            f"  prints locally."
        )
    else:
        print("  Confirm it is live by running the agent once and checking the logs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
