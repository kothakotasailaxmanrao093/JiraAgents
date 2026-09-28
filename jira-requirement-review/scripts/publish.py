"""Publish this agent. Run from the agent directory:

    uv run python scripts/publish.py --bump patch

Everything is enforced by the shared implementation in ``../scripts/publish.py``
— shared so that all agents publish through exactly the same checks, and a fix
to the procedure cannot be applied to one agent and missed on another.

Run it with ``--help`` to see every option, or ``--dry-run`` to run all the
checks without uploading.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

IMPLEMENTATION = Path(__file__).resolve().parents[2] / "scripts" / "publish.py"

if not IMPLEMENTATION.exists():
    sys.exit(f"Cannot find the publish implementation at {IMPLEMENTATION}")

runpy.run_path(str(IMPLEMENTATION), run_name="__main__")
