"""One structured log line per stage: what ran, for which run, how long, ok.

    stage=read_jira_issue run=build-a1b2c3d4 seconds=1.52 ok=True issue=BGV-25 attachments=2

This is how speed is measured rather than guessed. A stage is timed in the
activity (tool) that does the work — never in the workflow, which may not read
the clock — and the run is the Temporal workflow id, which carries the
router's run id ("build-<run_id>"), so one grep follows a request end to end.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from common_lib.utils.logger import setup_logger

logger = setup_logger(__name__)


def run_label() -> str:
    """The Temporal workflow id of the running activity, or "-" outside one."""
    try:
        from temporalio import activity

        return activity.info().workflow_id
    except Exception:  # noqa: BLE001 — outside an activity (tests, local runs)
        return "-"


@contextmanager
def stage(name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time the block and log it once. The yielded dict takes extra fields.

    ``ok`` is False only when the block raised; a block that returns a
    handled failure is still a stage that ran, and says so in its own fields.
    """
    start = time.perf_counter()
    ok = True
    try:
        yield fields
    except BaseException:
        ok = False
        raise
    finally:
        extra = " ".join(f"{key}={value}" for key, value in fields.items())
        logger.info(
            f"stage={name} run={run_label()} seconds={time.perf_counter() - start:.2f} "
            f"ok={ok} {extra}".rstrip()
        )
