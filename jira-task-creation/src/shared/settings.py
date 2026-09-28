# GENERATED FILE — DO NOT EDIT.
# Mirrored from the canonical shared/ package by scripts/sync_shared.py.
# Edit shared/settings.py at the repository root and re-run that script.
"""Reading configuration from the environment, in one place.

Every setting an agent has is an environment variable, so that a deployment is
configured the same way whether it runs locally, in a container, or on the
Aetherion platform. What each variable *means* stays next to the code that uses
it — ``max_pages()`` belongs with the Confluence client, not here. What lives
here is how a value is turned into a bool, an int or a list, because that was
written out by hand in seven places and drifted.

Shared by every Jira agent: each one keeps its own variable *names* (``LTW_``,
``REVIEW_``) and passes them in, so moving these readers here changed no
deployment's configuration.

Every reader is forgiving on purpose: a stray space, a capital letter or a
missing value must never take a deployment down, and a value that cannot be
parsed falls back to the default with a warning rather than raising.
"""

from __future__ import annotations

import os

from ._logging import get_logger

logger = get_logger(__name__)

# What counts as yes and no. Accepting only "true" meant a deployment that set
# "1" or "yes" silently got the default instead.
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    """An integer, or ``default`` when unset, blank or unparseable."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"{name} is not an integer ({raw!r}); using {default}")
        return default
    if minimum is not None and value < minimum:
        logger.warning(f"{name}={value} is below the minimum {minimum}; using {minimum}")
        return minimum
    return value


def env_float(name: str, default: float) -> float:
    """A float, or ``default`` when unset, blank or unparseable."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"{name} is not a number ({raw!r}); using {default}")
        return default


def env_bool(name: str, default: bool = False) -> bool:
    """A boolean, accepting the spellings people actually write.

    Anything unrecognised falls back to ``default`` rather than being read as
    false, so a typo in a safety flag cannot quietly switch a guard off.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    logger.warning(f"{name}={raw!r} is not a yes/no value; using {default}")
    return default


def env_list(name: str, separator: str = ",") -> list[str]:
    """A separated list, with blanks and surrounding spaces dropped."""
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(separator) if item.strip()]


def mask(secret: str | None, reveal: int = 0) -> str:
    """Show only the shape of a secret in logs — never the whole value.

    ``reveal`` is how many characters to show at each end. It defaults to zero
    because that is the safe answer; a caller that genuinely needs to tell two
    credentials apart in a log asks for it explicitly. There used to be two
    copies of this, one in ``jira.client`` revealing three characters and one
    in ``notifications.email`` revealing none — which meant a change to how
    secrets are logged could be made in one and missed in the other.
    """
    if not secret:
        return "<empty>"
    if reveal <= 0 or len(secret) <= reveal * 2:
        return f"<set,len={len(secret)}>"
    return f"{secret[:reveal]}…{secret[-reveal:]} (len={len(secret)})"
