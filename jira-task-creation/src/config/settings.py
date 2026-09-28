"""Reading configuration from the environment, in one place.

The readers themselves now live in :mod:`src.shared.settings`, shared with the
other Jira agents — they were identical in spirit in every project and drifted
(there were two ``mask`` implementations revealing different numbers of
characters). This module stays as the import site every caller already uses, so
moving them changed no call site and no behaviour.

What each variable *means* still stays next to the code that uses it:
``max_pages()`` belongs with the Confluence client, not here.
"""

from __future__ import annotations

from src.shared.settings import env_bool, env_float, env_int, env_list, mask

__all__ = ["env_bool", "env_float", "env_int", "env_list", "mask"]
