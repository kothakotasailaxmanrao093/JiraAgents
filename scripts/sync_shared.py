"""Mirror the canonical ``shared/`` package into each agent project.

Why a mirror at all — the Aetherion packer builds one tarball per project from
that project's ``src/`` directory, so a package that only exists in a sibling
folder simply is not deployed. Publishing ``shared`` as a wheel would avoid the
copy, but needs an index and credentials this project does not have today.

So: **one canonical definition** in ``shared/``, mirrored into each project's
``src/shared/``, with :mod:`tests.test_shared_in_sync` failing the build the
moment a copy drifts. The copies are build output that happens to be checked in.
Never edit ``<project>/src/shared/`` — edit ``shared/`` and run this.

Usage:
    python scripts/sync_shared.py            # write the mirrors
    python scripts/sync_shared.py --check    # verify only; non-zero if stale
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "shared"

# Every project that vendors the shared package. A fourth agent is one line.
TARGETS = ("jira-task-creation", "jira-requirement-review", "jira-orchestration")

MANIFEST = "_manifest.json"

BANNER = (
    "# GENERATED FILE — DO NOT EDIT.\n"
    "# Mirrored from the canonical shared/ package by scripts/sync_shared.py.\n"
    "# Edit shared/{name} at the repository root and re-run that script.\n"
)

# A local ``__init__.py`` is REQUIRED and must stay EMPTY. Both halves matter:
#
#   * required — ``aetherion_sdk.cli._detect_packages()`` only finds directories
#     under ``src/`` that contain an ``__init__.py``. Without this file the whole
#     shared package is silently absent from the deploy tarball, and every agent
#     fails on the worker with ImportError while working perfectly in tests.
#   * empty — the packer's GLOBAL_IGNORE then *drops* it from that same tarball,
#     so anything defined here exists locally and is missing in production.
#
# Import the modules directly (``from shared import adf``), never names from the
# package itself. Same rule, and same reason, as src/jira/__init__.py.
INIT_STUB = '''"""Namespace only — see scripts/sync_shared.py for why this file is empty.

Required so the Aetherion packer detects ``shared`` as a package; dropped from
the deploy tarball, so nothing may be defined here. Import the modules:

    from shared import adf, attachments      # jira-requirement-review (flat root)
    from src.shared import adf, attachments  # jira-task-creation (src root)
"""
'''


def _sources() -> list[Path]:
    """The shared modules, excluding anything the packer would drop anyway."""
    return sorted(p for p in CANONICAL.glob("*.py") if p.name != "__init__.py")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _rendered(path: Path) -> str:
    """The file as it should appear in a mirror: banner + canonical body."""
    return BANNER.format(name=path.name) + path.read_text()


def sync(check_only: bool = False) -> int:
    sources = _sources()
    if not sources:
        print(f"No shared modules found in {CANONICAL}", file=sys.stderr)
        return 1

    manifest = {p.name: _digest(_rendered(p)) for p in sources}
    stale: list[str] = []

    for project in TARGETS:
        dest = ROOT / project / "src" / "shared"
        if not (ROOT / project / "src").is_dir():
            print(f"skip {project}: no src/ directory", file=sys.stderr)
            continue
        dest.mkdir(parents=True, exist_ok=True)

        for src in sources:
            target = dest / src.name
            want = _rendered(src)
            if target.exists() and target.read_text() == want:
                continue
            stale.append(f"{project}/src/shared/{src.name}")
            if not check_only:
                target.write_text(want)

        # Without this the packer never detects `shared`; with anything *in* it,
        # production breaks. See INIT_STUB above.
        init = dest / "__init__.py"
        if not init.exists() or init.read_text() != INIT_STUB:
            stale.append(f"{project}/src/shared/__init__.py")
            if not check_only:
                init.write_text(INIT_STUB)

        # Remove a module deleted from the canonical package.
        keep = {p.name for p in sources} | {MANIFEST, "__init__.py"}
        for extra in dest.glob("*.py"):
            if extra.name not in keep:
                stale.append(f"{project}/src/shared/{extra.name} (removed)")
                if not check_only:
                    extra.unlink()

        manifest_path = dest / MANIFEST
        want_manifest = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        if not manifest_path.exists() or manifest_path.read_text() != want_manifest:
            if not check_only:
                manifest_path.write_text(want_manifest)
            elif f"{project}/src/shared/{MANIFEST}" not in stale:
                stale.append(f"{project}/src/shared/{MANIFEST}")

    if check_only:
        if stale:
            print("Shared package is OUT OF SYNC:", file=sys.stderr)
            for item in stale:
                print(f"  {item}", file=sys.stderr)
            print("\nRun: python scripts/sync_shared.py", file=sys.stderr)
            return 1
        print(f"Shared package in sync across {len(TARGETS)} project(s).")
        return 0

    print(f"Synced {len(sources)} module(s) into {len(TARGETS)} project(s):")
    for name in sorted(manifest):
        print(f"  {name}")
    if stale:
        print(f"\nUpdated {len(stale)} file(s).")
    else:
        print("\nAlready up to date.")
    return 0


def main() -> int:
    return sync(check_only="--check" in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())


# Kept for callers that want the copy without the CLI (the drift tests).
def canonical_digests() -> dict[str, str]:
    return {p.name: _digest(_rendered(p)) for p in _sources()}


__all__ = ["sync", "canonical_digests", "CANONICAL", "TARGETS", "MANIFEST"]

