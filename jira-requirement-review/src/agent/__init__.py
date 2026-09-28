"""Agent package.

Kept import-light: the SDK-bound agents (``review_agent``, ``post_agent``) are
registered by the worker's auto-discovery, which imports the agent modules
directly. The pure ``review_flow`` / ``post_flow`` modules carry the orchestration
logic and no SDK imports, so they can be imported and unit-tested without the
Aetherion runtime.
"""
