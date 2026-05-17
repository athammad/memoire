# Project Cognition Engine

## Overview

Project Cognition Engine is a local-first persistent memory system for developers working with AI assistants.

The system runs as a background daemon inside a project directory. It automatically observes project activity — file changes, AI actions, git events — and builds structured memory in a local database. AI assistants (Claude Code) then query that memory at session start instead of re-reading files from scratch.

The objective is to transform stateless AI assistants into persistent collaborators with durable project understanding.

---

# Core Problem

Claude Code and similar AI assistants are stateless across sessions. Every new session requires re-reading the same files, re-establishing the same context, re-discovering the same architectural decisions.

This causes:
- excessive token usage,
- slow session startup,
- repeated file reads,
- lost discoveries from prior sessions,
- inability to build on previous work.

---

# Vision

A background process that silently watches project activity and builds a structured memory. When a new AI session starts, it queries that memory and arrives with full project context — no file re-reading required.

> Install it. Forget about it. Your AI assistant remembers everything.

---

# How It Works

```text
Developer works on project
         ↓
  Two input streams:
         ↓
┌─────────────────────────────────┐
│ 1. Claude Code hooks            │
│    (PostToolUse events)         │
│    captures AI file reads,      │
│    edits, discoveries           │
├─────────────────────────────────┤
│ 2. File system watcher          │
│    (watchdog)                   │
│    captures file saves,         │
│    git commits, project changes │
└─────────────────────────────────┘
         ↓
  Background Daemon
  processes events,
  extracts knowledge
         ↓
      SurrealDB
  (structured memory)
         ↓
  MCP Server
  serves context to Claude
         ↓
  New Claude session starts
  with full project context
  — no file re-reading
```

---

# Design Principles

## 1. Fully Automatic

No manual commands required. The daemon runs in the background and captures everything silently.

---

## 2. Local-First

The system runs entirely on the developer's machine:
- no cloud dependency,
- no data leaves the project,
- privacy-preserving,
- low cost.

---

## 3. Namespace Isolation

Each project has its own isolated memory namespace. Installing the daemon in `~/projects/project_a` never pollutes memory for `~/projects/project_b`.

Project identity is determined by the directory the daemon is initialized in.

---

## 4. Structured Cognition

The system stores structured knowledge, not raw conversation dumps:
- entities (files, modules, services, concepts),
- relationships between entities,
- important events (refactors, discoveries, decisions),
- document summaries.

---

## 5. Token Efficiency

When Claude queries memory, it receives compressed, structured context — not raw file contents. This minimizes token usage while maximizing useful context.

---

# Core Components

## 1. Background Daemon

A persistent background process installed inside a project directory.

Input streams:
- **Claude Code hooks** — PostToolUse events reporting every file Claude reads, edits, or runs
- **File system watcher** — file saves, git commits, project-level changes

Responsibilities:
- receive raw activity events,
- extract entities and relationships,
- score event importance,
- store structured memory in SurrealDB,
- deduplicate and consolidate over time.

---

## 2. MCP Server

Exposes project memory as tools Claude Code can call natively.

This is the read interface — how Claude queries memory at session start instead of re-reading files.

Example tools exposed:
- `get_project_context` — returns compressed project overview
- `get_entity(name)` — returns what is known about a specific entity
- `get_recent_events` — returns recent important discoveries
- `search_memory(query)` — full-text search across stored knowledge

---

## 3. Knowledge Graph

Stores relationships between project entities in SurrealDB using native graph edges (`RELATE`).

Examples:
```text
(AuthService) --DEPENDS_ON--> (Database)
(LoginFlow) --USES--> (JWTModule)
(ApiGateway) --ROUTES_TO--> (UserService)
```

---

## 4. Episodic Memory

Stores important project events with importance scores.

Examples:
- refactors,
- architectural decisions,
- debugging discoveries,
- performance findings,
- migration events.

Example:
```json
{
  "summary": "Switched auth from JWT to session-based",
  "importance": 0.92,
  "related_entities": ["AuthService", "SessionManager"],
  "timestamp": "2026-05-17T14:00:00Z"
}
```

---

## 5. Document Memory

Stores summaries of key project files and documentation.

Rather than storing raw file contents, the daemon stores compressed summaries with extracted entities and tags. This keeps memory token-efficient.

---

# Database Architecture

## Storage Backend

Primary backend: **SurrealDB**

SurrealDB is chosen because it:
- ships as a single binary (no separate server process),
- is multi-model: documents, graph relationships, and full-text search in one system,
- can run embedded in-process,
- has near-zero setup friction.

Full-text search (BM25, built into SurrealDB) is used for memory retrieval. No external embedding service required.

---

## Database Structure

Single database:
```text
project_memory
```

Tables:
```text
projects
documents
entities
relationships
events
```

Graph edges use SurrealDB's native `RELATE` statement.

All records are scoped by `project_id` for namespace isolation.

---

# Python SDK

The Python SDK wraps the SurrealDB client and provides the cognition logic used by both the daemon and the MCP server.

## Dependencies

```bash
pip install surrealdb watchdog
```

## Layering

```text
Daemon / MCP Server
        ↓
  Memory SDK (this project)
        ↓
  surrealdb (PyPI client)
        ↓
  SurrealDB (local binary)
```

## Core API

```python
memory.store_entity(name, type, summary, project_id)
memory.store_relationship(source, relation, target, project_id)
memory.store_event(summary, importance, entities, project_id)
memory.store_document(title, summary, tags, project_id)
memory.search(query, project_id)
memory.get_context(project_id)
```

---

# Project Initialization

Inside a project directory:

```bash
memory init
```

This:
- creates `.memory/config.json` with the project namespace,
- starts the background daemon,
- installs Claude Code hooks (PostToolUse) into `.claude/settings.json`,
- starts the MCP server.

After `memory init`, everything runs automatically.

---

# Claude Code Integration

## Hooks (Write Path)

Claude Code hooks are configured automatically by `memory init` in `.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "memory hook-event"
          }
        ]
      }
    ]
  }
}
```

Every Claude action (file read, edit, bash run) fires the hook, which sends the event to the daemon.

## MCP Server (Read Path)

The MCP server is registered with Claude Code so Claude can query memory as a native tool at session start.

---

# MVP Phases

## Phase 1 — Core Loop

The minimum viable product is the complete write → store → read loop:

- background daemon with file watcher
- Claude Code hook integration
- SurrealDB storage (entities, relationships, events, documents)
- MCP server with basic context retrieval
- `memory init` setup command
- full-text search retrieval

This phase delivers the core value: Claude arrives with project context without re-reading files.

---

## Phase 2 — Intelligence Layer

- importance scoring for events,
- memory consolidation and deduplication,
- smarter entity extraction,
- relationship graph evolution,
- context compression.

---

## Phase 3 — Ecosystem

- multi-agent memory sharing,
- collaborative memory across team members,
- distributed synchronization,
- IDE integrations beyond Claude Code.

---

# Core Value Proposition

Current AI assistants:
- re-read the same files every session,
- waste tokens on context reconstruction,
- lose discoveries between sessions,
- have no memory of architectural decisions.

Project Cognition Engine:
- runs silently in the background,
- builds structured project memory automatically,
- serves compressed context to Claude at session start,
- makes every session feel like Claude never left.

> Install it once. Your AI assistant remembers everything.
