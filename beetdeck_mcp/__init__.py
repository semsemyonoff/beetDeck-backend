"""MCP server exposing the beetDeck REST API to an AI agent.

Runs as its own process (`python -m beetdeck_mcp`) from the same install as the
Flask app and talks to it over HTTP — it must never import Flask request state.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
