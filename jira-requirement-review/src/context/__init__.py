"""Pluggable context sources.

A ``ContextSource`` turns some external input into labeled ``ContextDocument``s
that the LLM reasons over and cites. Add new sources (Confluence, Slack, ...) by
implementing the ABC and registering them in ``registry.build_sources`` — no
orchestrator or prompt changes required.
"""
