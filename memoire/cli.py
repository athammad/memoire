"""CLI entry point — memoire init, start, hook-event, mcp."""

import asyncio
import hashlib
import json
import os
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
    "memory",
]

# Supported providers and their defaults
_PROVIDER_CHOICES = ["claude", "cursor", "windsurf", "codex", "gemini", "ollama"]

_LLM_DEFAULTS = {
    "claude":   {"llm": "claude",    "llm_model": "claude-haiku-4-5"},
    "cursor":   {"llm": "anthropic", "llm_model": "claude-haiku-4-5-20251001"},
    "windsurf": {"llm": "anthropic", "llm_model": "claude-haiku-4-5-20251001"},
    "codex":    {"llm": "openai",    "llm_model": "gpt-4o-mini"},
    "gemini":   {"llm": "gemini",    "llm_model": "gemini-2.0-flash"},
    "ollama":   {"llm": "ollama",    "llm_model": "llama3"},
}

# Providers that have IDE integration (instructions file + MCP config)
_IDE_PROVIDERS = {"claude", "cursor", "windsurf", "codex", "gemini"}

# Instructions text — same content for every provider, adapted to each format
_INSTRUCTIONS = """\
## Project Memory (memoire)
This project has memoire installed. At the start of every session:
1. Call the `get_context` MCP tool to load the causal project graph — do NOT read files to establish context.
2. Call `expand(path)` on any directory or file before opening it — get causal context without a file read.
3. Use `search(query)` to find entities, documents, or relationships before grepping or reading files.
4. Use `recent_events` to see what changed recently.

The causal graph tells you *what will break if something changes* — use it before touching anything.
Only read a file directly if memoire's context is genuinely insufficient for the specific task.
"""

_INSTRUCTIONS_MARKER = "## Project Memory (memoire)"


def _find_surreal_binary() -> str | None:
    """Check default install locations used by the SurrealDB installer."""
    candidates = [
        Path.home() / ".surrealdb" / "surreal",
        Path("/usr/local/bin/surreal"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


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

    Returns True if reachable after this call.
    Exits with a helpful message if the binary is missing.
    """
    if _surreal_reachable():
        return True

    binary = shutil.which("surreal") or _find_surreal_binary()
    if not binary:
        click.echo(
            f"[memoire] SurrealDB is not installed.\n"
            f"[memoire] Install it from: {_SURREAL_INSTALL_URL}"
        )
        sys.exit(1)

    click.echo("[memoire] SurrealDB not running — starting it...")
    cmd = [binary] + _SURREAL_START_CMD[1:]
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

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
    """memoire — persistent causal project memory for AI coding assistants."""


@main.command()
@click.option("--project-root", default=None, help="Project root directory (default: cwd)")
@click.option(
    "--provider",
    default="claude",
    type=click.Choice(_PROVIDER_CHOICES, case_sensitive=False),
    help=(
        "AI provider: claude (default, CLAUDE.md + hooks + MCP), "
        "cursor (.cursor/rules/ + MCP), windsurf (.windsurfrules + MCP), "
        "codex (AGENTS.md + MCP, OpenAI Codex CLI), "
        "gemini (GEMINI.md + MCP, Google Gemini CLI), "
        "ollama (LLM extraction only, filesystem watcher for activity)."
    ),
)
@click.option("--model", default=None, help="Override the default LLM model for this provider.")
def init(project_root: str | None, provider: str, model: str | None) -> None:
    """Initialise memoire in this project and wire up the chosen provider."""
    root = Path(project_root).resolve() if project_root else Path.cwd()
    memory_dir = root / ".memory"
    memory_dir.mkdir(exist_ok=True)

    config_path = memory_dir / "config.json"
    if config_path.exists():
        existing = json.loads(config_path.read_text())
        project_id = existing["project_id"]
        click.echo(f"[memoire] Already initialised. project_id={project_id}")
    else:
        project_id = _project_id_from_path(root)
        click.echo(f"[memoire] Created .memory/config.json — project_id={project_id}")

    llm_cfg = dict(_LLM_DEFAULTS[provider])
    if model:
        llm_cfg["llm_model"] = model

    config = {
        "project_id": project_id,
        "provider": provider,
        **llm_cfg,
    }
    config_path.write_text(json.dumps(config, indent=2))

    _ensure_surreal_running()
    asyncio.run(_init_schema())
    click.echo("[memoire] SurrealDB schema ready.")

    if provider == "claude":
        _configure_claude_code(root)
    elif provider == "cursor":
        _configure_cursor(root)
    elif provider == "windsurf":
        _configure_windsurf(root)
    elif provider == "codex":
        _configure_codex(root)
    elif provider == "gemini":
        _configure_gemini_cli(root)
    else:
        # ollama — server only, no IDE integration or instructions file
        click.echo(
            "[memoire] Ollama provider — no IDE integration configured. "
            "The filesystem watcher will track file changes."
        )
        _log_api_key_hint(provider)

    count = asyncio.run(quick_scan(root, project_id))
    click.echo(f"[memoire] Registered {count} files.")
    click.echo("[memoire] Done.")
    if count > 0:
        click.echo("[memoire] Next: run 'memoire ingest' to build the causal graph from existing files.")
    else:
        click.echo("[memoire] Project is empty — no ingest needed yet.")
    click.echo("[memoire] Run 'memoire start' to begin the background daemon (watches for file changes).")


async def _init_schema() -> None:
    async with get_db() as db:
        await ensure_schema(db)


def _log_api_key_hint(provider: str) -> None:
    """Print the expected environment variable for the given LLM provider."""
    hints = {
        "codex":  "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "ollama": None,
    }
    var = hints.get(provider)
    if var:
        set_hint = "already set" if os.environ.get(var) else f"not set — export {var}=..."
        click.echo(f"[memoire] LLM extraction uses {provider} ({var}: {set_hint})")
    elif provider == "ollama":
        click.echo("[memoire] LLM extraction uses Ollama at http://localhost:11434")


# ---------------------------------------------------------------------------
# Provider-specific configuration writers
# ---------------------------------------------------------------------------

def _register_mcp_globally(provider: str) -> None:
    """Register the memoire MCP server using the provider's CLI tool if available."""
    # Each provider CLI that supports `mcp add` style registration
    cli_cmds: dict[str, list[str]] = {
        "claude":   ["claude", "mcp", "add", "--scope", "user", "memoire-ai", "memoire", "mcp"],
        "cursor":   [],   # Cursor has no CLI for MCP registration — config file only
        "windsurf": [],   # Windsurf has no CLI for MCP registration — config file only
        "codex":    [],   # Codex CLI has no MCP add command — config file only
        "gemini":   [],   # Gemini CLI has no MCP add command — config file only
        "ollama":   [],   # Ollama has no MCP integration
    }
    cmd = cli_cmds.get(provider, [])
    if not cmd:
        return
    if not shutil.which(cmd[0]):
        return
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        click.echo(f"[memoire] Registered MCP server globally via {cmd[0]}")
    else:
        manual = " ".join(cmd)
        click.echo(f"[memoire] Could not register MCP globally — run manually: {manual}")


def _write_instructions_file(path: Path, content: str, label: str) -> None:
    """Write instructions file, prepending to existing content if already present."""
    if path.exists():
        existing = path.read_text()
        if _INSTRUCTIONS_MARKER in existing:
            click.echo(f"[memoire] {label} already contains memoire instructions.")
            return
        path.write_text(content + "\n---\n\n" + existing.lstrip())
    else:
        path.write_text(content)
    click.echo(f"[memoire] Wrote memoire instructions to {path.name}")


def _configure_claude_code(root: Path) -> None:
    """Configure Claude Code: CLAUDE.md + PostToolUse/PreToolUse hooks + MCP."""
    claude_dir = root / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_path = claude_dir / "settings.json"

    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}

    hooks = settings.setdefault("hooks", {})

    post_tool = hooks.setdefault("PostToolUse", [])
    hook_entry = {"matcher": "*", "hooks": [{"type": "command", "command": "memoire hook-event"}]}
    if hook_entry not in post_tool:
        post_tool.append(hook_entry)
        click.echo("[memoire] Added PostToolUse hook to .claude/settings.json")

    pre_tool = hooks.setdefault("PreToolUse", [])
    pre_hook_entry = {"matcher": "Read", "hooks": [{"type": "command", "command": "memoire pre-read"}]}
    if pre_hook_entry not in pre_tool:
        pre_tool.append(pre_hook_entry)
        click.echo("[memoire] Added PreToolUse hook to .claude/settings.json")

    mcp_servers = settings.setdefault("mcpServers", {})
    if "memoire-ai" not in mcp_servers:
        mcp_servers["memoire-ai"] = {"command": "memoire", "args": ["mcp"]}
        click.echo("[memoire] Added MCP server to .claude/settings.json")

    settings_path.write_text(json.dumps(settings, indent=2))

    _register_mcp_globally("claude")
    _write_instructions_file(root / "CLAUDE.md", _INSTRUCTIONS, "CLAUDE.md")


def _configure_cursor(root: Path) -> None:
    """Configure Cursor: .cursor/rules/memoire.mdc + .cursor/mcp.json."""
    cursor_dir = root / ".cursor"
    cursor_dir.mkdir(exist_ok=True)

    # Rules file — Cursor MDC format with frontmatter
    rules_dir = cursor_dir / "rules"
    rules_dir.mkdir(exist_ok=True)
    rules_path = rules_dir / "memoire.mdc"
    mdc_content = (
        "---\n"
        "description: Use memoire causal memory for project context\n"
        "alwaysApply: true\n"
        "---\n\n"
        + _INSTRUCTIONS
    )
    if not rules_path.exists():
        rules_path.write_text(mdc_content)
        click.echo("[memoire] Created .cursor/rules/memoire.mdc")
    else:
        click.echo("[memoire] .cursor/rules/memoire.mdc already exists.")

    # MCP config
    mcp_path = cursor_dir / "mcp.json"
    mcp_config = json.loads(mcp_path.read_text()) if mcp_path.exists() else {}
    servers = mcp_config.setdefault("mcpServers", {})
    if "memoire-ai" not in servers:
        servers["memoire-ai"] = {"command": "memoire", "args": ["mcp"]}
        mcp_path.write_text(json.dumps(mcp_config, indent=2))
        click.echo("[memoire] Added MCP server to .cursor/mcp.json")

    _register_mcp_globally("cursor")
    click.echo("[memoire] Cursor configured. Set ANTHROPIC_API_KEY for markdown extraction.")


def _configure_windsurf(root: Path) -> None:
    """Configure Windsurf: .windsurfrules + global MCP config."""
    _write_instructions_file(root / ".windsurfrules", _INSTRUCTIONS, ".windsurfrules")

    # Windsurf MCP config — global user-level file
    mcp_path = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_config = json.loads(mcp_path.read_text()) if mcp_path.exists() else {}
    servers = mcp_config.setdefault("mcpServers", {})
    if "memoire-ai" not in servers:
        servers["memoire-ai"] = {"command": "memoire", "args": ["mcp"]}
        mcp_path.write_text(json.dumps(mcp_config, indent=2))
        click.echo(f"[memoire] Added MCP server to {mcp_path}")

    _register_mcp_globally("windsurf")
    click.echo("[memoire] Windsurf configured. Set ANTHROPIC_API_KEY for markdown extraction.")


def _configure_codex(root: Path) -> None:
    """Configure OpenAI Codex CLI: AGENTS.md + MCP server config."""
    _write_instructions_file(root / "AGENTS.md", _INSTRUCTIONS, "AGENTS.md")

    # Codex CLI reads MCP servers from .codex/config.toml or ~/.codex/config.toml
    codex_dir = root / ".codex"
    codex_dir.mkdir(exist_ok=True)
    config_path = codex_dir / "config.toml"

    # Read existing TOML or start fresh — use simple string manipulation to stay
    # dependency-free (avoid requiring the toml package)
    existing = config_path.read_text() if config_path.exists() else ""
    mcp_block = (
        '\n[[mcpServers]]\n'
        'name = "memoire-ai"\n'
        'command = "memoire"\n'
        'args = ["mcp"]\n'
    )
    if "memoire-ai" not in existing:
        config_path.write_text(existing + mcp_block)
        click.echo("[memoire] Added MCP server to .codex/config.toml")

    _register_mcp_globally("codex")
    click.echo("[memoire] Codex CLI configured. Set OPENAI_API_KEY for markdown extraction.")


def _configure_gemini_cli(root: Path) -> None:
    """Configure Google Gemini CLI: GEMINI.md + MCP server config."""
    _write_instructions_file(root / "GEMINI.md", _INSTRUCTIONS, "GEMINI.md")

    # Gemini CLI reads MCP servers from .gemini/settings.json (project) or
    # ~/.gemini/settings.json (global). We write the project-level file.
    gemini_dir = root / ".gemini"
    gemini_dir.mkdir(exist_ok=True)
    settings_path = gemini_dir / "settings.json"

    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    servers = settings.setdefault("mcpServers", {})
    if "memoire-ai" not in servers:
        servers["memoire-ai"] = {"command": "memoire", "args": ["mcp"]}
        settings_path.write_text(json.dumps(settings, indent=2))
        click.echo("[memoire] Added MCP server to .gemini/settings.json")

    _register_mcp_globally("gemini")
    click.echo("[memoire] Gemini CLI configured. Set GEMINI_API_KEY for markdown extraction.")


# ---------------------------------------------------------------------------
# Standard commands
# ---------------------------------------------------------------------------

@main.command()
@click.option("--project-root", default=None, help="Project root directory (default: auto-detect)")
def ingest(project_root: str | None) -> None:
    """Deep-read existing project files and populate the causal graph."""
    root = Path(project_root).resolve() if project_root else _find_project_root()
    config_path = root / ".memory" / "config.json"
    if not config_path.exists():
        click.echo("[memoire] Not initialised. Run 'memoire init' first.")
        sys.exit(1)

    project_id = json.loads(config_path.read_text())["project_id"]
    _ensure_surreal_running()

    config = json.loads(config_path.read_text())
    llm = config.get("llm", "claude")
    if llm in {"anthropic", "openai", "gemini"}:
        key_map = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
        var = key_map[llm]
        if not os.environ.get(var):
            click.echo(f"[memoire] Warning: {var} is not set — markdown LLM extraction will be skipped.")

    click.echo(f"[memoire] Deep ingesting {root} ...")
    docs, code = asyncio.run(deep_ingest(root, project_id))
    click.echo(f"[memoire] Done — {docs} documents ingested, {code} code files registered.")
    if docs == 0 and code == 0:
        click.echo("[memoire] No files found. Make sure you are in the correct project directory.")


@main.command()
@click.option("--project-root", default=None, help="Project root directory (default: auto-detect)")
@click.option("--foreground", is_flag=True, default=False, help="Run in the foreground instead of daemonizing.")
def start(project_root: str | None, foreground: bool) -> None:
    """Start the background daemon for this project (daemonizes by default)."""
    root = Path(project_root).resolve() if project_root else _find_project_root()
    _ensure_surreal_running()

    if foreground:
        from .daemon import run as run_daemon
        click.echo(f"[memoire] Starting daemon for {root} (foreground)")
        run_daemon(root)
        return

    # Daemonize: re-launch self as a detached subprocess and return immediately.
    log_path = root / ".memory" / "daemon.log"
    log_file = open(log_path, "a")
    subprocess.Popen(
        [sys.executable, "-m", "memoire.cli", "start", "--foreground", "--project-root", str(root)],
        stdout=log_file,
        stderr=log_file,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    click.echo(f"[memoire] Daemon started. Logs: {log_path}")


@main.command()
@click.option("--project-root", default=None, help="Project root directory (default: auto-detect)")
def stop(project_root: str | None) -> None:
    """Stop the running daemon for this project."""
    import signal

    root = Path(project_root).resolve() if project_root else _find_project_root()
    pid_path = root / ".memory" / "daemon.pid"

    if not pid_path.exists():
        click.echo("[memoire] No daemon PID file found — is the daemon running?")
        sys.exit(1)

    pid = int(pid_path.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        pid_path.unlink(missing_ok=True)
        click.echo(f"[memoire] Daemon stopped (pid {pid}).")
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        click.echo(f"[memoire] Daemon (pid {pid}) was not running. Cleaned up PID file.")


@main.command("install-service")
@click.option("--project-root", default=None, help="Project root directory (default: auto-detect)")
def install_service(project_root: str | None) -> None:
    """Install the daemon as a system service that starts automatically on login."""
    import platform

    root = Path(project_root).resolve() if project_root else _find_project_root()
    config_path = root / ".memory" / "config.json"
    if not config_path.exists():
        click.echo("[memoire] Not initialised. Run 'memoire init' first.")
        sys.exit(1)

    config = json.loads(config_path.read_text())
    project_id = config["project_id"]
    memoire_bin = shutil.which("memoire") or sys.executable + " -m memoire.cli"
    log_path = root / ".memory" / "daemon.log"

    system = platform.system()
    if system == "Linux":
        _install_systemd_service(project_id, root, memoire_bin, log_path)
    elif system == "Darwin":
        _install_launchagent(project_id, root, memoire_bin, log_path)
    else:
        click.echo(f"[memoire] Unsupported platform: {system}. Set up the daemon manually.")
        sys.exit(1)


def _install_systemd_service(project_id: str, root: Path, memoire_bin: str, log_path: Path) -> None:
    """Create and enable a systemd user service for the daemon."""
    service_name = f"memoire-{project_id}"
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_path = service_dir / f"{service_name}.service"

    unit = (
        "[Unit]\n"
        f"Description=memoire daemon for {root.name}\n"
        "After=default.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={root}\n"
        f"ExecStart={memoire_bin} start --foreground --project-root {root}\n"
        f"StandardOutput=append:{log_path}\n"
        f"StandardError=append:{log_path}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    service_path.write_text(unit)

    ret = subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    if ret.returncode != 0:
        click.echo("[memoire] Failed to reload systemd. Is systemd running?")
        sys.exit(1)

    subprocess.run(["systemctl", "--user", "enable", "--now", service_name], check=True)
    click.echo(f"[memoire] Service installed and started: {service_name}")
    click.echo(f"[memoire] Logs: {log_path}")
    click.echo(f"[memoire] To check status: systemctl --user status {service_name}")
    click.echo(f"[memoire] To uninstall:    memoire uninstall-service")


def _install_launchagent(project_id: str, root: Path, memoire_bin: str, log_path: Path) -> None:
    """Create and load a launchd user agent for the daemon (macOS)."""
    label = f"ai.memoire.{project_id}"
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_path = agents_dir / f"{label}.plist"

    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f'  <key>Label</key><string>{label}</string>\n'
        '  <key>ProgramArguments</key>\n  <array>\n'
        f'    <string>{memoire_bin}</string>\n'
        '    <string>start</string><string>--foreground</string>\n'
        f'    <string>--project-root</string><string>{root}</string>\n'
        '  </array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>KeepAlive</key><true/>\n'
        f'  <key>StandardOutPath</key><string>{log_path}</string>\n'
        f'  <key>StandardErrorPath</key><string>{log_path}</string>\n'
        '</dict>\n</plist>\n'
    )
    plist_path.write_text(plist)

    subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=True)
    click.echo(f"[memoire] LaunchAgent installed and loaded: {label}")
    click.echo(f"[memoire] Logs: {log_path}")
    click.echo(f"[memoire] To uninstall: memoire uninstall-service")


@main.command("uninstall-service")
@click.option("--project-root", default=None, help="Project root directory (default: auto-detect)")
def uninstall_service(project_root: str | None) -> None:
    """Remove the system service installed by install-service."""
    import platform

    root = Path(project_root).resolve() if project_root else _find_project_root()
    config_path = root / ".memory" / "config.json"
    if not config_path.exists():
        click.echo("[memoire] Not initialised.")
        sys.exit(1)

    project_id = json.loads(config_path.read_text())["project_id"]
    system = platform.system()

    if system == "Linux":
        service_name = f"memoire-{project_id}"
        subprocess.run(["systemctl", "--user", "disable", "--now", service_name], check=False)
        service_path = Path.home() / ".config" / "systemd" / "user" / f"{service_name}.service"
        service_path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        click.echo(f"[memoire] Service removed: {service_name}")
    elif system == "Darwin":
        label = f"ai.memoire.{project_id}"
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        subprocess.run(["launchctl", "unload", "-w", str(plist_path)], check=False)
        plist_path.unlink(missing_ok=True)
        click.echo(f"[memoire] LaunchAgent removed: {label}")
    else:
        click.echo(f"[memoire] Unsupported platform: {system}.")
        sys.exit(1)


@main.command("pre-read")
def pre_read() -> None:
    """
    PreToolUse hook — remind Claude to use expand() before reading a file.

    Reads the hook event from stdin, extracts the file path, and prints a
    reminder to stdout so Claude sees it before the Read tool executes.
    Always exits 0 (never blocks the read).
    """
    try:
        payload = json.loads(sys.stdin.read())
        file_path = payload.get("tool_input", {}).get("file_path", "")
        if file_path:
            print(
                f"[memoire] Before reading '{file_path}', consider calling "
                f"expand('{file_path}') to get causal context and content "
                f"without opening the file."
            )
    except Exception:
        pass
    sys.exit(0)


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
        pass
    sys.exit(0)


@main.command()
def mcp() -> None:
    """Start the MCP server (called by the IDE automatically)."""
    from .mcp_server import run as run_mcp
    run_mcp()


@main.command("check")
@click.option("--project-root", default=None, help="Project root directory (default: auto-detect)")
def check(project_root: str | None) -> None:
    """Diagnose the memoire setup in this project."""
    root = Path(project_root).resolve() if project_root else _find_project_root()
    ok = True

    # 1. Config file
    config_path = root / ".memory" / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        provider = config.get("provider", "unknown")
        project_id = config.get("project_id", "unknown")
        click.echo(f"[check] ✓ .memory/config.json  (project_id={project_id}, provider={provider})")
    else:
        click.echo("[check] ✗ .memory/config.json missing — run 'memoire init'")
        ok = False
        provider = None

    # 2. SurrealDB binary
    if shutil.which("surreal") or _find_surreal_binary():
        click.echo("[check] ✓ SurrealDB binary found")
    else:
        click.echo(f"[check] ✗ SurrealDB not installed — {_SURREAL_INSTALL_URL}")
        ok = False

    # 3. SurrealDB reachable
    if _surreal_reachable():
        click.echo("[check] ✓ SurrealDB is reachable")
    else:
        click.echo("[check] ✗ SurrealDB is not running — run 'memoire start' or start it manually")
        ok = False

    # 4. Provider-specific files
    if provider == "claude":
        _check_file(root / "CLAUDE.md", "CLAUDE.md")
        _check_file(root / ".claude" / "settings.json", ".claude/settings.json")
        settings_path = root / ".claude" / "settings.json"
        if settings_path.exists():
            s = json.loads(settings_path.read_text())
            servers = s.get("mcpServers", {})
            if "memoire-ai" in servers:
                click.echo("[check] ✓ MCP server registered in .claude/settings.json")
            else:
                click.echo("[check] ✗ MCP server missing from .claude/settings.json — re-run 'memoire init'")
                ok = False
    elif provider == "cursor":
        _check_file(root / ".cursor" / "rules" / "memoire.mdc", ".cursor/rules/memoire.mdc")
        _check_file(root / ".cursor" / "mcp.json", ".cursor/mcp.json")
    elif provider == "windsurf":
        _check_file(root / ".windsurfrules", ".windsurfrules")
        mcp_path = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
        _check_file(mcp_path, "~/.codeium/windsurf/mcp_config.json")
    elif provider == "codex":
        _check_file(root / "AGENTS.md", "AGENTS.md")
        _check_file(root / ".codex" / "config.toml", ".codex/config.toml")
    elif provider == "gemini":
        _check_file(root / "GEMINI.md", "GEMINI.md")
        _check_file(root / ".gemini" / "settings.json", ".gemini/settings.json")

    # 5. API key for the configured LLM backend
    if config_path.exists():
        llm = config.get("llm", "claude")
        _check_api_key(llm)

    # 6. Graph populated?
    if ok:
        try:
            pid = config["project_id"]

            async def _count() -> int:
                async with get_db() as db:
                    result = await db.query(
                        "SELECT count() FROM entities WHERE project_id = $pid GROUP ALL",
                        {"pid": pid},
                    )
                    row = result[0] if result else {}
                    return row.get("count", 0) if isinstance(row, dict) else 0

            n = asyncio.run(_count())
            if n > 0:
                click.echo(f"[check] ✓ Graph has {n} entities — ingest complete")
            else:
                click.echo("[check] ✗ Graph is empty — run 'memoire ingest' to build the causal graph")
                ok = False
        except Exception as e:
            click.echo(f"[check] ? Could not query graph: {e}")

    click.echo("")
    if ok:
        click.echo("[check] All checks passed.")
    else:
        click.echo("[check] Some checks failed — see above.")
        sys.exit(1)


def _check_file(path: Path, label: str) -> bool:
    """Print a pass/fail line for a file existence check. Returns True if present."""
    if path.exists():
        click.echo(f"[check] ✓ {label}")
        return True
    click.echo(f"[check] ✗ {label} missing")
    return False


def _check_api_key(llm: str) -> None:
    """Warn if the expected API key for the LLM backend is not set."""
    key_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    var = key_map.get(llm)
    if var:
        if os.environ.get(var):
            click.echo(f"[check] ✓ {var} is set")
        else:
            click.echo(f"[check] ✗ {var} is not set — markdown LLM extraction will fail")
    elif llm == "ollama":
        click.echo("[check] ✓ Ollama — no API key required")
