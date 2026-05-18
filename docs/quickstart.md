# Installation & Quick Start

## Prerequisites

- Python 3.12+
- [SurrealDB](https://surrealdb.com/install)

```bash
curl -sSf https://install.surrealdb.com | sh
```

SurrealDB is started automatically by memoire when needed — you don't have to manage it manually.

---

## Install

```bash
pip install memoire-ai
```

---

## Initialise your project

Run `memoire init` from your project root. Pick the provider that matches your AI assistant:

=== "Claude Code"

    ```bash
    memoire init --provider claude
    ```

    Creates `CLAUDE.md`, configures `.claude/settings.json` with PostToolUse/PreToolUse hooks and the MCP server. Uses the `claude` CLI for markdown extraction (no API key needed).

=== "Cursor"

    ```bash
    export ANTHROPIC_API_KEY=sk-ant-...
    memoire init --provider cursor
    ```

    Creates `.cursor/rules/memoire.mdc` and `.cursor/mcp.json`.

=== "Windsurf"

    ```bash
    export ANTHROPIC_API_KEY=sk-ant-...
    memoire init --provider windsurf
    ```

    Creates `.windsurfrules` and `~/.codeium/windsurf/mcp_config.json`.

=== "Codex CLI"

    ```bash
    export OPENAI_API_KEY=sk-...
    memoire init --provider codex
    ```

    Creates `AGENTS.md` and `.codex/config.toml`.

=== "Gemini CLI"

    ```bash
    export GEMINI_API_KEY=...
    memoire init --provider gemini
    ```

    Creates `GEMINI.md` and `.gemini/settings.json`.

=== "Ollama"

    ```bash
    memoire init --provider ollama
    ```

    No instructions file or IDE integration — uses the filesystem watcher and local Ollama for markdown extraction.

---

## Build the causal graph

```bash
memoire ingest
```

**Only needed if your project already has files.** If the folder is empty, skip this step — the daemon will pick up files as you create them.

Reads every file in your project, extracts imports, inheritance, side-effect categories, mutation patterns, and test assertion edges. For markdown files, calls your configured LLM to extract intentional causal relationships (SPECIFIES, IMPLEMENTS, DRIVES, DOCUMENTS).

This takes a few seconds to a few minutes depending on project size and how many markdown files need LLM extraction.

---

## Start the daemon

```bash
memoire install-service
```

Registers a systemd user service (Linux) or LaunchAgent (macOS). The daemon starts automatically on every login, restarts if it crashes, and works with any project — Ruby, Go, TypeScript, Python, whatever. Run this once and never think about it again.

The daemon watches for file changes and Claude Code hook events. It keeps the graph current as you work — no manual re-ingest needed.

Logs go to `.memory/daemon.log`. To check it:

```bash
# Linux
systemctl --user status memoire-<project_id>

# macOS
tail -f .memory/daemon.log
```

To stop or remove the service:

```bash
memoire stop              # stop the running daemon
memoire uninstall-service # remove the service entirely
```

If you don't want a system service, `memoire start` daemonizes without installing anything — it runs until the next reboot.

---

## Open a session

Open your IDE and start a new session. The assistant calls `get_context` automatically (via the MCP server) and arrives with the full causal model loaded.

---

## Commands reference

| Command | Description |
|---|---|
| `memoire init [--provider P] [--model M]` | Initialise memoire for the given provider |
| `memoire ingest` | Deep-read all files and build the causal graph |
| `memoire start` | Start the background daemon (daemonizes — survives terminal close) |
| `memoire stop` | Stop the running daemon |
| `memoire install-service` | Install the daemon as a system service (starts automatically on login) |
| `memoire uninstall-service` | Remove the system service |
| `memoire check` | Diagnose the setup — SurrealDB, config, provider files, API key, graph state |
| `memoire mcp` | Start the MCP server (called automatically by the IDE) |
| `memoire hook-event` | Forward a hook event to the daemon (called by IDE hooks) |
| `memoire pre-read` | Remind the assistant to use `expand()` before reading (Claude Code only) |

---

## Project files

```
.memory/
  config.json         # project_id, provider, llm settings
CLAUDE.md             # (or AGENTS.md / GEMINI.md / .windsurfrules / .cursorrules)
.claude/
  settings.json       # hooks + MCP server (Claude Code only)
```

!!! warning "Don't commit `.memory/` or the instructions file"
    Add `.memory/` and your instructions file (`CLAUDE.md`, `AGENTS.md`, etc.) to `.gitignore` if you don't want to share memoire configuration with your team. Or commit them — memoire handles shared project IDs gracefully.

---

## Troubleshooting

Run `memoire check` to diagnose your setup:

```
$ memoire check
[check] ✓ .memory/config.json  (project_id=myproject_a1b2c3, provider=claude)
[check] ✓ SurrealDB binary found
[check] ✓ SurrealDB is reachable
[check] ✓ CLAUDE.md
[check] ✓ .claude/settings.json
[check] ✓ MCP server registered in .claude/settings.json
[check] ✓ Graph has 42 entities — ingest complete

[check] All checks passed.
```

Common failures and fixes:

| Failure | Fix |
|---|---|
| `.memory/config.json` missing | Run `memoire init --provider <name>` |
| SurrealDB not installed | `curl -sSf https://surrealdb.com/install \| sh` |
| SurrealDB not running | Run `memoire start` (starts it automatically) |
| API key not set | `export ANTHROPIC_API_KEY=...` (or the relevant key) |
| Graph is empty | Run `memoire ingest` |
| Daemon not running after reboot | Run `memoire install-service` once to register it as a system service |
| Hook events not received | Check `.memory/daemon.log` — port 7892 may be in use by a stale process |
