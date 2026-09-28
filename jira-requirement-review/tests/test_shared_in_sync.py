"""The vendored shared package must match its canonical source.

``src/shared/`` is a mirror, not a source file. The Aetherion packer builds one
tarball per project from that project's ``src/``, so a package living only in a
sibling directory would never deploy — hence the copy. The copy is only safe if
drifting from the canonical version is impossible to miss, which is this test's
whole job.

Two failure modes, two checks:

* someone edited ``src/shared/<module>.py`` directly — caught by the manifest,
  which travels with the mirror and so works in a checkout of this project alone.
* someone edited the canonical ``shared/<module>.py`` and forgot to re-run the
  sync — caught by comparing against the canonical tree when it is reachable.

Either way the fix is the same, and the failure message says so.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
MIRROR = PROJECT / "src" / "shared"
CANONICAL = PROJECT.parent / "shared"

FIX = "Run: python scripts/sync_shared.py (from the repository root)"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def test_the_mirror_exists() -> None:
    assert MIRROR.is_dir(), f"{MIRROR} is missing. {FIX}"


def test_the_package_marker_exists_and_is_empty() -> None:
    """``__init__.py`` must be present locally and define nothing.

    Present: ``aetherion_sdk.cli._detect_packages()`` only finds directories
    under ``src/`` that contain one, so without it the whole shared package is
    silently absent from the deploy tarball — tests pass, production fails.

    Empty: the packer's ``GLOBAL_IGNORE`` then drops it from that same tarball,
    so anything defined here would exist locally and be missing on the worker.
    """
    init = MIRROR / "__init__.py"
    assert init.exists(), f"{init} is missing — the packer would not ship shared/. {FIX}"

    body = [
        line
        for line in init.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # Only the module docstring may remain.
    assert all(
        not line.startswith(("import ", "from ", "def ", "class ")) for line in body
    ), f"{init} must stay empty — the packer drops it from the deploy tarball."


def test_every_mirrored_module_matches_its_manifest() -> None:
    manifest_path = MIRROR / "_manifest.json"
    assert manifest_path.exists(), f"{manifest_path} is missing. {FIX}"
    manifest = json.loads(manifest_path.read_text())

    mirrored = {p.name for p in MIRROR.glob("*.py")} - {"__init__.py"}
    assert mirrored == set(manifest), (
        f"Mirrored modules {sorted(mirrored)} do not match the manifest "
        f"{sorted(manifest)}. {FIX}"
    )

    for name, expected in sorted(manifest.items()):
        actual = _digest((MIRROR / name).read_text())
        assert actual == expected, (
            f"src/shared/{name} has been edited directly. It is generated — "
            f"change shared/{name} at the repository root instead. {FIX}"
        )


def test_the_mirror_matches_the_canonical_package() -> None:
    """Catches a canonical edit that was never synced."""
    if not CANONICAL.is_dir():
        pytest.skip("canonical shared/ not reachable from this checkout")

    canonical = {p.name for p in CANONICAL.glob("*.py")} - {"__init__.py"}
    mirrored = {p.name for p in MIRROR.glob("*.py")} - {"__init__.py"}
    assert (
        canonical == mirrored
    ), f"canonical has {sorted(canonical)}, mirror has {sorted(mirrored)}. {FIX}"

    for name in sorted(canonical):
        want = (CANONICAL / name).read_text()
        got = (MIRROR / name).read_text()
        # The mirror carries a generated banner; the body must be identical.
        assert got.endswith(want), f"shared/{name} has changed since the last sync. {FIX}"
