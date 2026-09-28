"""Build the deploy tarball locally, without uploading it.

`aetherion publish` packs the project, uploads it, then deletes the artifact —
so there is no way to see what actually gets shipped. This script runs the same
packer the CLI uses and stops before the upload, leaving the tarball in `dist/`
and printing its manifest.

Usage:
    python scripts/build_package.py            # build + list contents
    python scripts/build_package.py --extract  # also unpack into dist/package/
"""

from __future__ import annotations

import importlib.metadata
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from aetherion_sdk import cli  # noqa: E402
from aetherion_sdk.packer import Packer  # noqa: E402


def build(dist: Path) -> Path:
    packages = cli._detect_packages()
    print(f"Detected packages: {', '.join(packages)}")

    # The CLI pins the SDK it was invoked with; Packer's own default is stale.
    packer = Packer(sdk_version=importlib.metadata.version("aetherion-sdk"))

    files = []
    for package in packages:
        files.extend(packer.collect_all_files("src", package))
    files.extend(packer._collect_root_files(".", ""))

    dist.mkdir(parents=True, exist_ok=True)
    return Path(packer.create_tar("package.tar.gz", files, str(dist)))


def report(archive: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        width = max(len(m.name) for m in members)
        print(f"\n{archive} ({archive.stat().st_size:,} bytes, {len(members)} files)\n")
        for member in sorted(members, key=lambda m: m.name):
            print(f"  {member.name:<{width}}  {member.size:>8,}")


def main() -> int:
    if Path.cwd() != ROOT:
        print(f"Run this from the project root ({ROOT}).", file=sys.stderr)
        return 1

    dist = ROOT / "dist"
    archive = build(dist)
    report(archive)

    if "--extract" in sys.argv:
        target = dist / "package"
        shutil.rmtree(target, ignore_errors=True)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(target, filter="data")
        print(f"\nExtracted to {target}")
        print(
            "\nThis is build OUTPUT, not source. It is a copy of what was shipped\n"
            "and it looks exactly like src/ — editing it changes nothing, and the\n"
            "next build overwrites it. Delete it when you are done reading it."
        )
        env_copy = target / ".env"
        if env_copy.exists():
            print(
                f"\n  ⚠  {env_copy} contains your real credentials, in plain text.\n"
                "     Do not share this folder. Run scripts/preflight_publish.py to\n"
                "     see what publishing would ship, with secrets masked."
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
