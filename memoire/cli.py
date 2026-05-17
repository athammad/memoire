"""CLI entry point — memoire init, start, hook-event, mcp."""

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import click
import httpx

from .db import DAEMON_PORT, SURREAL_URL, SURREAL_USER, SURREAL_PASS, get_db, ensure_schema
from .ingester import quick_scan, deep_ingest

_SURREAL_INSTALL_URL = "https://surrealdb.com/install"
_SURREAL_START_CMD = [
    "surreal", "start",
    "--user", SURREAL_USER,
    "--pass", SURREAL_PASS,
    "--bind", "0.0.0.0:8000",
    "memory",  # file-based storage in ./memory.db
]


def _surreal_reachable() -> bool:
    """Return True if SurrealDB is already accepting connections."""
    try:
        import asyncio as _asyncio
        from surrealdb.connections.async_ws import AsyncWsSurrealConnection

        async def _ping() -> None:
            async with AsyncWsSurrealConnection(SURREAL_URL) as db:
                await db.connect()

        _asyncio.run(_ping())
        return True
    except Exception:
        return False


def _ensure_surreal_running() -> bool:
    """
    Make sure SurrealDB is running.

    Returns True if reachable after this call, False if surreal binary not found.
    Exits with a helpful message if the binary is missing.
    """
    if _surreal_reachable():
        return True

    binary = shutil.which("surreal")
    if not binary:
        click.echo(
            f"[memoire] SurrealDB is not installed.\n"
            f"[memoire] Install it from: {_SURREAL_INSTALL_URL}"
        )
        sys.exit(1)

    click.echo("[memoire] SurrealDB not running — starting it...")
    subprocess.Popen(
        _SURREAL_START_CMD,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait up to 5 seconds for it to become reachable
    for _ in range(10):
        time.sleep(0.5)
        if _surreal_reachable():
            click.echo("[memoire] SurrealDB started.")
            return True

    click.echo("[memoire] SurrealDB did not start in time. Check logs.")
    sys.exit(1)


def _find_project_root() -> Path:
    """Walk up from cwd to find the nearest .memory/config.json."""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".memory" / "config.json").exists():
            return parent
    return current


def _project_id_from_path(path: Path) -> str:
    """Derive a stable project_id from the project root path."""
    name = path.name.lower().replace(" ", "_")
    suffix = hashlib.sha1(str(path).encode()).hexdigest()[:8]
    return f"{name}_{suffix}"


@click.group()
def main() -> None:
    """memoire — persistent project memory for Claude Code."""


@main.command()
@click.option("--project-root", default=None, help="Project root directory (default: cwd)")
def init(project_root: str | None) -> None:
    """Initialise memory for this project and wire up Claude Code integration."""
    root = Path(project_root).resolve() if project_root else Path.cwd()
    memory_dir = root / ".memory"
    memory_dir.mkdir(exist_ok=True)

    config_path = memory_dir / "config.json"
    if config_path.exists():
        project_id = json.loads(config_path.read_text())["project_id"]
        click.echo(f"[memoire] Already initialised. project_id={project_id}")
    else:
        project_id = _project_id_from_path(root)
        config_path.write_text(json.dumps({"project_id": project_id}, indent=2))
        click.echo(f"[memoire] Created .memory/config.json — project_id={project_id}")

    # Ensure SurrealDB is running then initialise schema
    _ensure_surreal_running()
    asyncio.run(_init_schema())
    click.echo("[memoire] SurrealDB schema ready.")

    # Wire up Claude Code integration
    _configure_claude_code(root)

    # Quick scan — register existing files as entities
    click.echo("[memoire] Scanning project files...")
    count = asyncio.run(quick_scan(root, project_id))
    click.echo(f"[memoire] Registered {count} files.")

    click.echo("[memoire] Done.")
    click.echo("[memoire] Run 'memoire ingest' to deep-read existing docs.")
    click.echo("[memoire] Run 'memoire start' to begin the background daemon.")


async def _init_schema() -> None:
    async with get_db() as db:
        await ensure_schema(db)


_CLAUDE_MD_BLOCK = """\
## Project Memory (memoire)
This project has memoire installed. At the start of every session:
1. Call the `get_context` MCP tool to load project memory — do NOT read files to establish context.
2. Use the `search` MCP tool to find specific entities, documents, or relationships before reading files.
3. Use `recent_events` to see what changed recently.

Only read a file directly if memoire's context is insufficient for the specific task.
"""

_CLAUDE_MD_MARKER = "## Project Memory (memoire)"


def _configure_claude_md(root: Path) -> None:
    """Create or update CLAUDE.md with memoire usage instructions."""
    claude_md = root / "CLAUDE.md"

    if claude_md.exists():
        content = claude_md.read_text()
        if _CLAUDE_MD_MARKER in content:
            return  # already configured
        # Prepend the block to existing content
        claude_md.write_text(_CLAUDE_MD_BLOCK + "\n---\n\n" + content.lstrip())
        click.echo("[memoire] Updated CLAUDE.md with memoire instructions.")
    else:
        claude_md.write_text(_CLAUDE_MD_BLOCK)
        click.echo("[memoire] Created CLAUDE.md with memoire instructions.")


def _configure_claude_code(root: Path) -> None:
    """Inject hook and MCP server config into .claude/settings.json."""
    claude_dir = root / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_path = claude_dir / "settings.json"

    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}

    # PostToolUse hook
    hooks = settings.setdefault("hooks", {})
    post_tool = hooks.setdefault("PostToolUse", [])
    hook_entry = {"matcher": "*", "hooks": [{"type": "command", "command": "memoire hook-event"}]}
    if hook_entry not in post_tool:
        post_tool.append(hook_entry)
        click.echo("[memoire] Added PostToolUse hook to .claude/settings.json")

    # MCP server
    mcp_servers = settings.setdefault("mcpServers", {})
    if "memoire-ai" not in mcp_servers:
        mcp_servers["memoire-ai"] = {"command": "memoire", "args": ["mcp"]}
        click.echo("[memoire] Added MCP server to .claude/settings.json")

    settings_path.write_text(json.dumps(settings, indent=2))

    # CLAUDE.md
    _configure_claude_md(root)


@main.command()
@click.option("--project-root", default=None, help="Project root directory (default: auto-detect)")
def ingest(project_root: str | None) -> None:
    """Deep-read existing project files and populate memory."""
    root = Path(project_root).resolve() if project_root else _find_project_root()
    config_path = root / ".memory" / "config.json"
    if not config_path.exists():
        click.echo("[memoire] Not initialised. Run 'memoire init' first.")
        sys.exit(1)

    project_id = json.loads(config_path.read_text())["project_id"]
    _ensure_surreal_running()

    click.echo(f"[memoire] Deep ingesting {root} ...")
    docs, code = asyncio.run(deep_ingest(root, project_id))
    click.echo(f"[memoire] Done — {docs} documents ingested, {code} code files registered.")


@main.command()
@click.option("--project-root", default=None, help="Project root directory (default: auto-detect)")
def start(project_root: str | None) -> None:
    """Start the background daemon for this project."""
    from .daemon import run as run_daemon

    root = Path(project_root).resolve() if project_root else _find_project_root()
    _ensure_surreal_running()
    click.echo(f"[memoire] Starting daemon for {root}")
    run_daemon(root)


@main.command("hook-event")
def hook_event() -> None:
    """Receive a Claude Code hook event from stdin and forward to the daemon."""
    try:
        payload = sys.stdin.read()
        if not payload.strip():
            sys.exit(0)
        httpx.post(
            f"http://127.0.0.1:{DAEMON_PORT}/hook",
            content=payload,
            headers={"Content-Type": "application/json"},
            timeout=1.0,
        )
    except Exception:
        # Never block Claude Code — fail silently if daemon is not running
        pass
    sys.exit(0)


@main.command()
def mcp() -> None:
    """Start the MCP server (called by Claude Code automatically)."""
    from .mcp_server import run as run_mcp
    run_mcp()
