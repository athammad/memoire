# Contributing

## Setup

```bash
git clone https://github.com/athammad/memoire
cd memoire
pip install -e ".[dev,docs]"
```

## Running tests

```bash
pytest tests/
```

All 135 tests are pure Python — no Rust, Java, Ruby, or C compiler needed. The extractors are regex-based and tested with string fixtures.

## Running docs locally

```bash
mkdocs serve
```

Opens the documentation site at `http://127.0.0.1:8000` with live reload.

## Project structure

```
memoire/
  cli.py          # CLI commands (init, ingest, start, stop, install-service, uninstall-service, check, mcp, hook-event, pre-read)
  db.py           # SurrealDB connection and schema management
  daemon.py       # Background daemon (filesystem watcher + hook event receiver)
  ingester.py     # Quick scan and deep ingest
  processor.py    # Static analysis extractors + LLM markdown extraction
  sdk.py          # Core graph operations (store, query, score, promote, detect cycles)
  mcp_server.py   # MCP server (get_context, expand, search, recent_events)
tests/
  test_extractors.py   # All unit tests
docs/                  # MkDocs documentation source
mkdocs.yml             # MkDocs configuration
```

## Adding a new language

1. Add side-effect patterns to `processor.py` as `_SIDE_EFFECT_PATTERNS_<LANG>`
2. Add a `_detect_side_effects_<lang>` function
3. Optionally add `_extract_state_mutations_<lang>` if the language has mutable instance state
4. Add `_extract_<lang>` for imports, inheritance, and test assertion edges
5. Add the suffix to `process_file`'s dispatch block (both side-effect and graph sections)
6. Update `_is_test_path` in `sdk.py` for the language's test file conventions
7. Add tests in `tests/test_extractors.py`
8. Update the language table in `docs/languages.md` and `README.md`

## Adding a new provider

1. Add the provider name to `_PROVIDER_CHOICES` in `cli.py`
2. Add LLM defaults to `_LLM_DEFAULTS`
3. Write a `_configure_<provider>` function that creates the right instructions file and MCP config
4. Add the provider to `init`'s dispatch block
5. If the provider uses a new LLM API, add a branch in `_call_llm` in `processor.py`
6. Update `docs/providers.md`

## Code style

- No comments unless the *why* is non-obvious
- No docstrings beyond one-line summaries
- Minimal changes — don't refactor adjacent code while fixing a bug
- All new extractors must have tests before merging
