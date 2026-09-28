"""Aetherion SDK project: Jira Requirement Review agents.

Kept import-light on purpose. Agents/tools are registered with the worker via
auto-discovery (``AETHERION_AUTO_DISCOVER``) which imports the agent/tool
modules; the pure ``review``/``jira``/``llm``/``context`` packages carry no SDK
imports so they can be unit-tested without the runtime installed.
"""
