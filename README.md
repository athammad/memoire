<p align="center">
  <img src="docs/assets/logo.png" alt="memoire" width="180"/>
</p>

<p align="center">
  <a href="https://pypi.org/project/memoire-ai"><img src="https://img.shields.io/pypi/v/memoire-ai?color=blue&label=PyPI" alt="PyPI"></a>
  <a href="https://pypi.org/project/memoire-ai"><img src="https://img.shields.io/pypi/pyversions/memoire-ai" alt="Python"></a>
  <a href="https://github.com/athammad/memoire/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
  <a href="https://athammad.github.io/memoire"><img src="https://img.shields.io/badge/docs-online-blueviolet" alt="Docs"></a>
</p>

<p align="center">
  <strong>Your project's causal memory. Builds itself. Never resets.</strong>
</p>
<p align="center">
  <em>Up to 87% fewer tokens per session &nbsp;·&nbsp; 0 file reads on session start &nbsp;·&nbsp; knows what breaks before you ask</em>
</p>

# memoire

Persistent causal memory for AI coding assistants. Install it once — your assistant arrives at every session knowing not just what your project contains, but why things exist, what causes what, where changes will propagate, and what will break.

Works with **Claude Code**, **Cursor**, **Windsurf**, **OpenAI Codex CLI**, **Gemini CLI**, and **Ollama**.

**Documentation:** https://athammad.github.io/memoire

---

## Install

**Step 1 — Install SurrealDB** (the database memoire runs on):

```bash
curl -sSf https://install.surrealdb.com | sh
```

**Step 2 — Install memoire:**

```bash
# Linux / Windows
pip install memoire-ai

# macOS
brew tap athammad/memoire
brew install memoire-ai
```

**Step 3 — Set up in your project** (run once, from your project root):

```bash
memoire init --provider claude   # or: cursor, windsurf, codex, gemini, ollama
memoire ingest                   # skip this if the project folder is empty
memoire install-service          # starts automatically on every login from now on
```

That's it. Open a new session in your IDE — the assistant loads the full causal graph automatically. In Claude Code, you can also type `/memoire` to load it manually.

> **Projects with PDFs or images** (design docs, diagrams): run `pip install "memoire-ai[pdf]"` before `memoire ingest`.

---

## The problem

Every AI coding session starts from zero. The assistant re-reads the same files, re-establishes the same context, re-discovers the same architecture. But the deeper problem is worse: even after re-reading everything, it still has to reason about impact from scratch — "if I change this function, what breaks?" — by reading code rather than understanding intent and consequence.

## The insight

A project has layers of causality. A design document specifies a module. That module drives its dependents. Changes cascade downward. And within code, a function that writes shared state causes silent failures in anything that reads it — failures that don't show up until runtime.

memoire builds a **causal knowledge graph** that captures this structure. Not just what imports what, but what causes what to change, what will fail if something breaks, and why files exist at all.

See the [Theory & Design docs](https://athammad.github.io/memoire/theory/) for the full design rationale.

## How it works

```
File changes + AI assistant activity
              ↓
      Background Daemon
   (watches files, captures hooks)
              ↓
          SurrealDB
    (local graph + full-text search)
              ↓
         MCP Server
              ↓
  Assistant starts session with
  full causal project model — instantly
```

The graph evolves continuously. Every time a file is saved or the assistant edits it, edges are re-observed and their confidence scores increase. Causal patterns that persist across many sessions become highly confident. Transient patterns fade.

## What you get

| | Without memoire | With memoire |
|---|---|---|
| **Session start** | Re-reads 20,000–50,000 tokens of files | Loads 2,000–9,000 token causal graph |
| **Impact analysis** | Opens files to reason about what breaks | Traverses graph edges — 0 file reads |
| **Token cost (28-file project)** | ~58,000 tok / session | ~7,400 tok (structural) · ~23,000 tok (causal) |
| **Token reduction** | baseline | **−87%** structural · **−60%** causal |
| **File reads on session start** | N (one per file) | **0** |
| **Works offline** | ✓ | ✓ (all data stored locally) |
| **Supports PDFs & images** | — | ✓ (diagrams, design docs) |

Break-even: roughly 3 sessions on a project with 15+ files. See the [full benchmark](https://athammad.github.io/memoire/benchmark/) for methodology and results.

## Relationship types

| Relation | Direction | Type | Meaning |
|---|---|---|---|
| `SPECIFIES` | idea → code | causal | this doc defines the intent this file implements |
| `IMPLEMENTS` | code → idea | causal | this file is the realization of that concept |
| `DRIVES` | core → dependent | causal | changing this will force changes in that |
| `DOCUMENTS` | doc → code | causal | this doc describes that file's behaviour |
| `ASSERTS_ON` | test → module | causal, high-cost | this test will fail if that module changes |
| `IMPORTS` | file → module | structural | static dependency, evidence for DRIVES |
| `INHERITS` | class → class | structural | inheritance hierarchy |
| `CONTAINS` | dir → child | structural | directory/file hierarchy |

Causal edges are ranked above structural ones. High-cost edges (`ASSERTS_ON`, side-effect chains) surface first — breakage there has real-world consequences.

## Provider support

| Provider | Instructions file | MCP config | Activity hooks | Markdown LLM |
|---|---|---|---|---|
| Claude Code | `CLAUDE.md` | `.claude/settings.json` | ✓ PostToolUse / PreToolUse | `claude --print` CLI |
| Cursor | `.cursor/rules/memoire.mdc` | `.cursor/mcp.json` | — | Anthropic API (`ANTHROPIC_API_KEY`) |
| Windsurf | `.windsurfrules` | `~/.codeium/windsurf/mcp_config.json` | — | Anthropic API (`ANTHROPIC_API_KEY`) |
| Codex CLI | `AGENTS.md` | `.codex/config.toml` | — | OpenAI API (`OPENAI_API_KEY`) |
| Gemini CLI | `GEMINI.md` | `.gemini/settings.json` | — | Google API (`GEMINI_API_KEY`) |
| Ollama | — | — | — | Local Ollama at port 11434 |

For providers without hooks (Cursor, Windsurf, Codex, Gemini, Ollama), the filesystem watcher tracks all file changes — activity-based temporal causality inference is unavailable but the full static analysis graph and LLM markdown extraction work identically.

**API keys** — set the relevant environment variable before running `memoire ingest`:
```bash
export ANTHROPIC_API_KEY=...   # cursor, windsurf
export OPENAI_API_KEY=...      # codex
export GEMINI_API_KEY=...      # gemini
# ollama needs no key — runs at http://localhost:11434
```

## Commands

| Command | Description |
|---|---|
| `memoire init` | Initialise memoire in the current project |
| `memoire ingest` | Deep-read existing files — build full causal knowledge graph |
| `memoire start` | Start the daemon (daemonizes — survives terminal close) |
| `memoire stop` | Stop the running daemon |
| `memoire install-service` | Install as a system service — auto-starts on every login (recommended) |
| `memoire uninstall-service` | Remove the system service |
| `memoire check` | Diagnose the memoire setup — SurrealDB, config, provider files, API key, graph state |
| `memoire hook-event` | Called automatically by Claude Code hooks (internal) |
| `memoire pre-read` | Called automatically before Claude reads a file (internal) |
| `memoire mcp` | Start the MCP server (called automatically by Claude Code) |

## Slash commands (Claude Code)

After `memoire init`, four slash commands are installed in `.claude/commands/`:

| Command | What it does |
|---|---|
| `/memoire` | Load the full causal graph — top relationships, project structure, recent events |
| `/memoire-search <query>` | Search the graph by keyword |
| `/memoire-expand <path>` | Show all relationships and metadata for a specific file |
| `/memoire-recent` | Show recent file changes and inferred causal edges |

These call the memoire MCP tools without reading any source files.

## What Claude can query

**`get_context()`** — hierarchical project overview: directory/file tree, causal relationships ranked by centrality and confidence, recent events. Call at session start.

**`expand(path)`** — full detail for a directory or file. Includes side-effect categories, mutable state attributes, all causal and structural relationships with their confidence scores.

**`search(query)`** — full-text search across all stored knowledge.

**`recent_events(limit)`** — what changed recently.

## Causal scoring

Nodes are ranked by a composite score:

- **BFS causal reachability × 2** — true downstream reach via graph traversal (not degree count). A node that causes changes in 10 files through a chain scores higher than one directly imported by 3. Root-cause nodes (specs, core modules) score highest.
- **Causal in-degree** — how many causes point at this node.
- **Side-effect cost** — files that do network calls, database writes, or file I/O score higher because their breakage has real-world consequences.
- **Recency** — time-decay with a 7-day half-life.
- **Access frequency** — how often Claude has read or edited this file.

Edges are ranked by:
- **Observations** — how many times this edge has been re-confirmed by reprocessing. Stable edges (seen 20+ times) rank above transient ones. This is how the graph learns.
- **Causal bonus** — causal edges rank above structural ones.
- **Cost bonus** — high-cost edges (`ASSERTS_ON`, side-effect chains) rank first.

## Language support

| Language | Side effects | Mutations | Imports | Inheritance | Test assertions |
|---|---|---|---|---|---|
| Python | ✓ | ✓ `self.attr` | ✓ | ✓ | ✓ `test_*.py`, `*_test.py`, `tests/` |
| TypeScript / JS | ✓ | ✓ `this.attr` | ✓ | ✓ `extends` / `implements` | ✓ `.test.ts`, `.spec.ts`, `__tests__/` |
| Go | ✓ | — | ✓ | — | ✓ `_test.go` |
| Rust | ✓ | ✓ `self.field` | ✓ `use` | ✓ `impl Trait for` | ✓ `_test.rs`, `tests/` |
| Java | ✓ | ✓ `this.field` | ✓ `import` | ✓ `extends` / `implements` | ✓ `*Test.java`, `src/test/` |
| Ruby | ✓ | ✓ `@attr` | ✓ `require` | ✓ `class < Parent` | ✓ `_spec.rb`, `_test.rb`, `spec/` |
| C / C++ | ✓ | — | ✓ `#include` | ✓ `: public` | ✓ `test_*.c`, `*_test.cpp` |
| Markdown / RST | — | — | — | — | — |

All languages feed into the same causal graph with the same promotion rules: high-fan-in IMPORTS → DRIVES, test imports → ASSERTS_ON, mutation sources → DRIVES to importers.

Markdown files use LLM extraction (provider-configurable) to produce intentional causal edges: SPECIFIES, IMPLEMENTS, DRIVES, DOCUMENTS.

## What gets stored

**From Python files:**
- Import dependencies (IMPORTS) and class inheritance (INHERITS)
- Side-effect categories detected by pattern: `network`, `file_io`, `subprocess`, `database`, `cache`
- Mutable state attributes (`self.attr = ...`) — used to infer DRIVES edges to importers
- Test files (matching `test_*.py`, `*_test.py`, or in `tests/`) emit `ASSERTS_ON` edges for everything they import

**From TypeScript / JS files:**
- Import dependencies (IMPORTS), class inheritance (INHERITS), interface implementation (IMPLEMENTS)
- Same five side-effect categories detected by pattern
- Mutable state attributes (`this.attr = ...`) — same mutation-driven DRIVES inference
- Test files (`.test.ts`, `.spec.ts`, `__tests__/`) emit `ASSERTS_ON` edges for everything they import

**From Go files:**
- Import dependencies (IMPORTS) from single imports and import blocks
- Side-effect categories: `network`, `file_io`, `subprocess`, `database`
- Test files (`_test.go`) emit `ASSERTS_ON` edges for everything they import

**From markdown files:**
- Full content stored and indexed for search
- Claude extracts causal relationships: SPECIFIES, IMPLEMENTS, DRIVES, DOCUMENTS — with a rationale for each

**From Claude Code activity:**
- Sequential file edits within 5 minutes generate inferred DRIVES edges, reinforced on repetition
- Bash commands (git, pip, npm, pytest, etc.) stored as episodic events
- Every file read or edit bumps `access_count` and `observations` on related edges

**Structural promotions (run after every ingest and every 10 file changes):**
- High-fan-in IMPORTS → promoted to DRIVES (modules imported by 3+ files are causal roots)
- Test IMPORTS → promoted to ASSERTS_ON (high-cost)
- Mutation sources with importers → promoted to DRIVES (mutation-driven dependency)

**Graph integrity (Phase 3):**
- Every edge carries `extracted_from` — the file that produced it via static analysis
- When a file is reprocessed, edges no longer present in it are pruned (deleted import → edge removed)
- When a file is deleted from disk, its entity and all edges touching it are removed instantly
- Cycle detection runs after every ingest and promotion batch — causal edges must form a DAG; violations are logged as warnings

## Storage

All data is stored locally in SurrealDB — nothing leaves your machine. Each project has an isolated namespace.

## Project structure

```
.memory/
  config.json        # project namespace
.claude/
  settings.json      # hooks + MCP server (managed by memoire)
CLAUDE.md            # instructions for Claude (managed by memoire)
```

## Testing

The extraction and scoring logic is covered by a unit test suite:

```bash
pip install -e ".[dev]"
pytest tests/
```

135 tests covering: test-path detection across all 7 languages, side-effect detection, state mutation extraction, static extractors for Python/TypeScript/JS/Go/Rust/Java/Ruby/C/C++, BFS causal reachability, causal scoring, cycle detection, and LLM response parsing.
