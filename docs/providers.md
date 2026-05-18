# Provider Setup

memoire supports six AI assistant providers. Each gets the same full causal graph and MCP tools — what differs is the instructions file format, MCP config location, and LLM used for markdown extraction.

## Overview

| Provider | Instructions file | MCP config | Activity hooks | Markdown LLM |
|---|---|---|---|---|
| Claude Code | `CLAUDE.md` | `.claude/settings.json` | ✓ PostToolUse / PreToolUse | `claude --print` CLI |
| Cursor | `.cursor/rules/memoire.mdc` | `.cursor/mcp.json` | — | Anthropic API |
| Windsurf | `.windsurfrules` | `~/.codeium/windsurf/mcp_config.json` | — | Anthropic API |
| Codex CLI | `AGENTS.md` | `.codex/config.toml` | — | OpenAI API |
| Gemini CLI | `GEMINI.md` | `.gemini/settings.json` | — | Google API |
| Ollama | — | — | — | Local Ollama |

---

## Claude Code

The most fully integrated provider. Claude Code's hook system lets memoire track every file read and edit, enabling temporal causality inference (sequential edits → inferred DRIVES edges).

```bash
memoire init --provider claude
```

**What gets created:**

- `CLAUDE.md` — instructs Claude to call `get_context` at session start and `expand()` before reading files
- `.claude/settings.json` — PostToolUse hook (captures all tool calls), PreToolUse hook on Read (nudges expand first), MCP server registration

**Markdown extraction:** uses the `claude --print` CLI — no API key required, uses Claude Code's existing authentication.

---

## Cursor

```bash
export ANTHROPIC_API_KEY=sk-ant-...
memoire init --provider cursor
```

**What gets created:**

- `.cursor/rules/memoire.mdc` — MDC format with `alwaysApply: true` frontmatter, same instructions as CLAUDE.md
- `.cursor/mcp.json` — registers the memoire MCP server

**Markdown extraction:** Anthropic Messages API. Requires `ANTHROPIC_API_KEY`.

!!! note
    Cursor does not currently expose a general hook system equivalent to Claude Code's PostToolUse. The filesystem watcher handles file change tracking. Temporal causality inference (sequential edit patterns) is not available.

---

## Windsurf

```bash
export ANTHROPIC_API_KEY=sk-ant-...
memoire init --provider windsurf
```

**What gets created:**

- `.windsurfrules` — project-level rules file, same instructions content
- `~/.codeium/windsurf/mcp_config.json` — global MCP server registration

**Markdown extraction:** Anthropic Messages API. Requires `ANTHROPIC_API_KEY`.

---

## OpenAI Codex CLI

```bash
export OPENAI_API_KEY=sk-...
memoire init --provider codex
```

**What gets created:**

- `AGENTS.md` — Codex CLI's project instructions file
- `.codex/config.toml` — MCP server registration

**Markdown extraction:** OpenAI Chat Completions API (`gpt-4o-mini` by default). Requires `OPENAI_API_KEY`.

Override the model:

```bash
memoire init --provider codex --model gpt-4o
```

---

## Google Gemini CLI

```bash
export GEMINI_API_KEY=...
memoire init --provider gemini
```

**What gets created:**

- `GEMINI.md` — Gemini CLI's project instructions file
- `.gemini/settings.json` — MCP server registration

**Markdown extraction:** Google Generative Language API (`gemini-1.5-flash` by default). Requires `GEMINI_API_KEY`.

Override the model:

```bash
memoire init --provider gemini --model gemini-1.5-pro
```

---

## Ollama

```bash
memoire init --provider ollama
```

Ollama is a local inference server, not an IDE assistant — it has no standard project instructions file or hook system. memoire uses it purely for markdown LLM extraction.

**What gets created:** nothing IDE-side. The filesystem watcher tracks all file changes.

**Markdown extraction:** local Ollama at `http://localhost:11434` (`llama3` by default). No API key needed.

Override the model or URL:

```bash
memoire init --provider ollama --model mistral
```

To use a non-default Ollama URL, set it in `.memory/config.json`:

```json
{
  "provider": "ollama",
  "llm": "ollama",
  "llm_model": "mistral",
  "ollama_url": "http://192.168.1.10:11434"
}
```

---

## Switching providers

Re-run `memoire init --provider <new>` at any time. The config in `.memory/config.json` is updated and the new provider's files are created. Existing files from the previous provider are not removed automatically.
