"""Minimal, self-contained agentic tool loop.

Lets the local model use tools (web search/fetch today; MCP/code later) via an
OpenAI tool-calling loop. Kept dependency-light and standalone so Virtual Worker can
be split into its own repo without pulling in the agentic-engine runtime.
"""
