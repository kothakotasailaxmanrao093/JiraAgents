"""Namespace only — the re-export facade lives in :mod:`src.jira.api`.

Keep this file empty. The Aetherion packer's ``GLOBAL_IGNORE`` drops every
``__init__.py`` from the deploy tarball, so anything defined here exists
locally and is missing on the worker: ``from src import jira`` still succeeds
there (implicit namespace package) but the module comes back empty, and the
first attribute access dies with ``AttributeError: module 'src.jira' has no
attribute ...``.

Import the facade explicitly instead::

    from src.jira import api as jira

Leaving this empty means local runs and tests see exactly what the worker
sees, so the next such mistake fails here rather than in production.
"""
