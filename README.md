# memoire

Persistent project memory for Claude Code. Install it once in a project and Claude never has to re-read the same files again.

## The problem

Every Claude Code session starts from zero. Claude re-reads the same files, re-establishes the same context, re-discovers the same architecture. Wasted tokens, slow startup, no continuity between sessions.

## How it works

memoire runs a background daemon in your project directory. It watches every file change and every Claude action, builds structured memory in a local database, and serves it back to Claude at the start of each new session via an MCP server.

```
File changes + Claude activity
            ↓
    Background Daemon
            ↓
        SurrealDB
    (local, on your machine)
            ↓
       MCP Server
            ↓
  Claude starts session with
  full project context — instantly
```

Two representations are stored for every file:
- **Document** — full text content, fully searchable
- **Graph** — entities and relationships extracted automatically (imports, inheritance for code; entities and relationships from markdown via Claude)

## Prerequisites

- Python 3.12+
- [Claude Code](https://claude.ai/code) (VS Code extension or CLI)
- [SurrealDB](https://surrealdb.com/install) — memoire will start it automatically if installed

Install SurrealDB:
```bash
curl -sSf https://install.surrealdb.com | sh
```

## Installation

```bash
pip install memoire-ai
```

## Quick start

```bash
# Inside any project directory
memoire init       # set up config, hooks, MCP server, scan files
memoire ingest     # deep-read existing files and build knowledge graph
memoire start      # start the background daemon
```

Then open a new Claude Code session in the same directory. Claude will call `get_context` automatically and arrive with full project understanding.

## What `memoire init` does

1. Creates `.memory/config.json` with a stable project namespace
2. Starts SurrealDB if installed but not running
3. Creates the database schema
4. Adds a `PostToolUse` hook to `.claude/settings.json` so every Claude action is captured
5. Registers the MCP server in `.claude/settings.json` so Claude can query memory
6. Creates or updates `CLAUDE.md` with instructions for Claude to use memoire
7. Scans all project files and registers them as entities

## Commands

| Command | Description |
|---|---|
| `memoire init` | Initialise memoire in the current project |
| `memoire ingest` | Deep-read existing files — full text + knowledge graph |
| `memoire start` | Start the background daemon |
| `memoire hook-event` | Called automatically by Claude Code hooks (internal) |
| `memoire mcp` | Start the MCP server (called automatically by Claude Code) |

## What Claude can query

The MCP server exposes three tools Claude calls natively:

**`get_context()`** — compressed project overview: entities, documents, relationships, recent events. Call at session start.

**`search(query)`** — full-text search across all stored knowledge. Use instead of grepping files.

**`recent_events(limit)`** — what changed recently in the project.

## What gets stored

**From code files** (Python, TypeScript, Go, etc.):
- File registered as entity
- Import dependencies extracted (`file.py` IMPORTS `module`)
- Class inheritance extracted (`ClassA` INHERITS `ClassB`)

**From markdown files** (`.md`, `.rst`):
- Full content stored and indexed
- Claude extracts entities and relationships from the text (uses your existing Claude Code auth — no API key needed)

**From Claude Code activity** (via hooks):
- Files Claude reads or edits are processed immediately
- Bash commands (git, pip, npm, etc.) stored as episodic events

## How Claude uses it

At the start of every session, Claude reads `CLAUDE.md` and sees:

```
Call get_context MCP tool first — do not read files to establish context.
```

Claude calls `get_context()`, receives structured project knowledge, and answers questions from memory. It only opens individual files when the task genuinely requires the raw source.

## Cost

memoire uses Claude (via the `claude` CLI you already have) only to extract relationships from markdown files during `memoire ingest` and when markdown files change. Code files use free pattern-based extraction.

The savings are substantial: instead of re-reading 20,000–50,000 tokens of files every session, Claude receives 2,000–5,000 tokens of compressed structured context. Break-even happens after 2–3 sessions on any project.

## Storage

All data is stored locally in SurrealDB — nothing leaves your machine. Each project has an isolated namespace so memory from one project never pollutes another.

## Content hashing

Files are only reprocessed when their content actually changes. Autosaves and formatting-only changes are skipped — no unnecessary Claude calls.

## Project structure

```
.memory/
  config.json        # project namespace
.claude/
  settings.json      # hooks + MCP server (managed by memoire)
CLAUDE.md            # instructions for Claude (managed by memoire)
```

## Roadmap

- **Phase 2** — memory consolidation, importance scoring, context compression
- **Phase 3** — multi-agent memory sharing, collaborative memory across teams, IDE integrations beyond Claude Code
