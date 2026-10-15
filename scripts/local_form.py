"""The Aetherion test form for one agent, with uploads kept on this laptop (2026-10-06).

The same form ``aetherion test`` opens (``aetherion_sdk.local_form``), but a
picked file is kept in ``.local-uploads/`` instead of being sent to the hosted
platform, which a laptop run cannot read back (see ``local_files.py``).

Run from the agent's folder, with that agent's Python:

    cd jira-task-creation && .venv/bin/python ../scripts/local_form.py --port 8766

``scripts/run_local.sh --form`` starts one per agent this way.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_files import install_form  # noqa: E402
from local_worker import src_packages  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    # The agent's own settings, as `aetherion test` loads them; the shell wins.
    load_dotenv(Path.cwd() / ".env", override=False)
    install_form()
    from aetherion_sdk.local_form import serve_test_form

    packages = src_packages(Path.cwd())
    print(f"form on http://{args.host}:{args.port} (uploads kept on this laptop)", flush=True)
    asyncio.run(serve_test_form(packages, host=args.host, port=args.port))


if __name__ == "__main__":
    main()
