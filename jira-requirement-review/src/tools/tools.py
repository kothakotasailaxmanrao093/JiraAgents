"""Tool aggregator.

Importing each tool module here ensures its ``@tool`` decorator registers with
the worker even if auto-discovery only imports this module. Only the tool shim
modules are imported — the internal ``jira``/``llm``/``context``/``review``
packages carry no decorators.
"""

from __future__ import annotations

from . import (  # noqa: F401
    analyze_requirement_tool,
    build_review_report_tool,
    gather_context_tool,
    jira_search_tool,
    post_review_tool,
    render_review_tool,
    send_review_report_email_tool,
)
