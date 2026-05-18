"""MCP server — exposes project memory as tools Claude Code can call."""

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .db import get_db, load_project_config
from .sdk import get_project_context, search_memory, get_recent_events, expand_node

mcp = FastMCP("memoire-ai")

# Loaded lazily on first tool call so imports don't fail outside a project dir
_project_id: str | None = None


def _get_project_id() -> str:
    global _project_id
    if _project_id is None:
        _project_id = load_project_config(Path.cwd())["project_id"]
    return _project_id


@mcp.tool()
async def get_context() -> str:
    """
    Return a hierarchical overview of the project.

    Call this at the start of every session instead of re-reading project files.
    Returns the directory/file tree (structure), semantic relationships between
    files (IMPORTS, INHERITS, etc.), and recent events.

    Use expand(path) to drill into any directory or file for full detail.
    """
    async with get_db() as db:
        ctx = await get_project_context(db, _get_project_id())
    return json.dumps(ctx, indent=2, default=str)


@mcp.tool()
async def expand(path: str) -> str:
    """
    Return full detail for a directory or file node in the project graph.

    For a directory: lists its children with their summaries.
    For a file: returns the summary, full document content (if stored), and
    all semantic relationships (IMPORTS, INHERITS, etc.) touching this file.

    Args:
        path: Relative path from project root, e.g. "memoire/db.py" or "memoire".
              Use "." for the project root.
    """
    async with get_db() as db:
        result = await expand_node(db, _get_project_id(), path)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def search(query: str) -> str:
    """
    Search project memory for entities, events, or documents matching a query.

    Args:
        query: The topic or keyword to search for.
    """
    async with get_db() as db:
        results = await search_memory(db, _get_project_id(), query)
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
async def recent_events(limit: int = 20) -> str:
    """
    Return the most recent events recorded in this project.

    Args:
        limit: Maximum number of events to return (default 20).
    """
    async with get_db() as db:
        events = await get_recent_events(db, _get_project_id(), limit=limit)
    return json.dumps(events, indent=2, default=str)


def run() -> None:
    """Start the MCP server (stdio transport for Claude Code)."""
    from .cli import _ensure_surreal_running
    _ensure_surreal_running()
    mcp.run(transport="stdio")
