"""Agent package.

Kept import-light: the SDK-bound agent (``review_agent``) is registered by the
worker's auto-discovery, which imports the agent module directly. The pure
``review_flow`` module carries the orchestration logic and no SDK imports, so it
can be imported and unit-tested without the Aetherion runtime.
"""
