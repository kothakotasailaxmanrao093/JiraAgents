"""The Jira side of the agent, split by job.

One module per concern, re-exported here so callers keep writing
``jira.create_hierarchy(...)`` without caring which file it lives in::

    from src.jira import api as jira

This facade deliberately is not ``__init__.py``: the Aetherion packer drops
every ``__init__.py`` from the deploy tarball, which would leave these names
missing on the worker. See :mod:`src.jira` for the full story.

* ``client``     - HTTP client, credentials, shared low-level helpers
* ``adf``        - Atlassian Document Format, both directions
* ``context``    - reading a project, its epics, boards and sprints
* ``issues``     - creating the hierarchy, idempotently
* ``duplicates`` - recognising work that already exists
* ``trigger``    - the webhook side: who asked and what was answered
"""

from __future__ import annotations

# Atlassian Document Format: reading it into text and building it back.
from src.jira.adf import (
    _plain_text,
    adf,
)

# HTTP client, credentials and the low-level helpers everything shares.
from src.jira.client import (
    IDEMPOTENCY_LABEL_PREFIX,
    _http_error_text,
    _mask,
    allowed_project_keys,
    available_issue_types,
    issue_type_names,
    jira_client,
    jira_creds,
    missing_issue_types,
    read_only,
    resolve_issue_types,
    target_project_key,
)

# Reading a project: its description, tickets, epics, boards and sprints.
from src.jira.context import (
    EpicValidationError,
    SprintUnavailable,
    active_sprint,
    add_to_sprint,
    fetch_epic,
    fetch_existing_issues,
    fetch_project,
    is_authenticated,
    resolve_board,
)

# Recognising work the project already has.
from src.jira.duplicates import (
    adjudication_enabled,
    adjudication_floor,
    best_overlap,
    build_overlap_report,
    comparable_issues,
    compare_closed_work,
    containment,
    duplicate_threshold,
    find_near_misses,
    find_overlaps,
    find_requirement_match,
    is_closed,
    is_request_ticket,
    similarity,
)

# Creating the Epic/Story/Sub-task hierarchy, idempotently.
from src.jira.issues import (
    _reuse,
    create_hierarchy,
    find_existing,
    idempotency_key,
    link_related,
)

# The webhook side: who asked, what they asked, and what was answered.
from src.jira.trigger import (
    AGENT_FOOTER,
    AGENT_SIGNATURE,
    _person,
    add_comment,
    answered_comment_ids,
    awaiting_label,
    download_attachment,
    fetch_issue,
    is_agent_comment,
    mark_comment_answered,
    mentions_trigger,
    outcome_comment,
    parse_comments,
    parse_history,
    parse_links,
    parse_worklogs,
    processed_label,
    set_labels,
    trigger_keyword,
)

__all__ = [
    "AGENT_FOOTER",
    "AGENT_SIGNATURE",
    "adjudication_enabled",
    "adjudication_floor",
    "find_near_misses",
    "compare_closed_work",
    "is_closed",
    "EpicValidationError",
    "IDEMPOTENCY_LABEL_PREFIX",
    "SprintUnavailable",
    "_http_error_text",
    "_mask",
    "_person",
    "_plain_text",
    "_reuse",
    "active_sprint",
    "add_comment",
    "add_to_sprint",
    "adf",
    "allowed_project_keys",
    "answered_comment_ids",
    "available_issue_types",
    "awaiting_label",
    "best_overlap",
    "build_overlap_report",
    "comparable_issues",
    "containment",
    "create_hierarchy",
    "link_related",
    "download_attachment",
    "duplicate_threshold",
    "fetch_epic",
    "fetch_existing_issues",
    "fetch_issue",
    "fetch_project",
    "find_existing",
    "find_overlaps",
    "find_requirement_match",
    "idempotency_key",
    "is_agent_comment",
    "is_authenticated",
    "is_request_ticket",
    "issue_type_names",
    "resolve_issue_types",
    "jira_client",
    "jira_creds",
    "mark_comment_answered",
    "mentions_trigger",
    "missing_issue_types",
    "outcome_comment",
    "parse_comments",
    "parse_history",
    "parse_links",
    "parse_worklogs",
    "processed_label",
    "read_only",
    "resolve_board",
    "set_labels",
    "similarity",
    "target_project_key",
    "trigger_keyword",
]
