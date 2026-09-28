# GENERATED FILE — DO NOT EDIT.
# Mirrored from the canonical shared/ package by scripts/sync_shared.py.
# Edit shared/_logging.py at the repository root and re-run that script.
"""One logger factory for the shared package.

The agents log differently: the work-breakdown agent uses the platform's
``common_lib.utils.logger.setup_logger`` (colourised, structured), while the review agent
deliberately stays framework-free so its pure layers can be unit-tested without
the Aetherion runtime installed.

Shared code has to satisfy both, so it asks for the platform logger and falls
back to the standard library when the runtime is absent. This keeps LTW's log
formatting exactly as it is today while leaving the shared package importable
in a bare virtualenv.
"""

from __future__ import annotations

import logging


def get_logger(name: str) -> logging.Logger:
    try:
        from common_lib.utils.logger import setup_logger
    except Exception:  # noqa: BLE001 — no runtime installed; stdlib is fine
        return logging.getLogger(name)
    return setup_logger(name)
