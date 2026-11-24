"""Start the Delivery Studio server.

    jira-orchestration/.venv/bin/python delivery-studio/backend/main.py [--port 8780]

Usually started by delivery-studio/start.sh, which also starts the agents.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from studio import config  # noqa: E402  (loads the env files first)
from studio import jobs  # noqa: E402
from studio.app import app  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Aetherion Delivery Studio")
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    # Every line a run prints or logs is shown with that run on the page.
    sys.stdout = jobs.Tee(sys.stdout)
    logging.getLogger().addHandler(jobs.LogHandler())
    logging.getLogger().setLevel(logging.INFO)
    import uvicorn

    print(f"Aetherion Delivery Studio: http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
