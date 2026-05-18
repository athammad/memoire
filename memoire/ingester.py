"""Project file ingestion — quick scan and deep ingest."""

import json
import logging
from pathlib import Path

from .db import get_db
from .processor import process_file

from .sdk import (
    store_entity,
    store_relationship,
    promote_high_fan_in_to_drives,
    promote_test_assertions,
    promote_mutation_drives,
    detect_causal_cycles,
)

log = logging.getLogger("[ingester]")

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".memory", ".claude", ".next", ".nuxt",
    "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

_SKIP_DIR_SUFFIXES = {".egg-info"}

_DOC_EXTENSIONS = {".md", ".rst", ".txt"}

_CONFIG_FILENAMES = {
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "go.sum",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example", "requirements.txt", "setup.py", "setup.cfg",
    ".gitignore", "tsconfig.json", "vite.config.ts", "vite.config.js",
}

_CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
    ".cpp", ".c", ".h", ".rb", ".php", ".swift", ".kt", ".cs",
    ".sql", ".sh", ".yaml", ".yml", ".toml", ".json",
}

_SKIP_FILENAMES = {".env", ".env.local", ".env.production", ".env.staging"}

_ALL_RELEVANT = _CODE_EXTENSIONS | _DOC_EXTENSIONS


async def _upsert_directory_hierarchy(db, root: Path, file_path: Path, project_id: str) -> None:
    """
    Ensure every ancestor directory of file_path exists as an entity and is
    linked via CONTAINS edges all the way from the project root (".").
    """
    rel = file_path.relative_to(root)
    parts = rel.parts

    await store_entity(db, project_id, ".", "directory", "Project root")

    prev = "."
    for i in range(len(parts) - 1):  # directory parts only
        dir_rel = str(Path(*parts[: i + 1]))
        await store_entity(db, project_id, dir_rel, "directory", f"Directory: {dir_rel}")
        await store_relationship(db, project_id, prev, "CONTAINS", dir_rel)
        prev = dir_rel

    # link last directory → file
    await store_relationship(db, project_id, prev, "CONTAINS", str(rel))


def _skip_dir(name: str) -> bool:
    if name in _SKIP_DIRS:
        return True
    if name.startswith(".") and name not in {".env.example"}:
        return True
    if any(name.endswith(s) for s in _SKIP_DIR_SUFFIXES):
        return True
    return False


def iter_project_files(root: Path):
    """Yield relevant project files, skipping noise directories."""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in _SKIP_FILENAMES:
            continue
        relative_parts = path.relative_to(root).parts
        if any(_skip_dir(part) for part in relative_parts[:-1]):
            continue
        yield path


async def quick_scan(root: Path, project_id: str) -> int:
    """
    Register every relevant file as an entity without reading contents.

    Fast — just paths. Called by `memoire init` so the DB is immediately
    aware of project structure. Returns the number of files registered.
    """
    count = 0
    async with get_db() as db:
        for path in iter_project_files(root):
            if path.suffix not in _ALL_RELEVANT and path.name not in _CONFIG_FILENAMES:
                continue
            rel = str(path.relative_to(root))
            await store_entity(db, project_id, rel, "file", f"File: {rel}")
            await _upsert_directory_hierarchy(db, root, path, project_id)
            count += 1
    return count


async def deep_ingest(root: Path, project_id: str) -> tuple[int, int]:
    """
    Full ingestion pass — processes every file into both document and graph form.

    Delegates to processor.process_file which handles:
      - document storage (full text, searchable)
      - graph extraction (imports, inheritance for code; LLM for markdown)

    Returns (docs_processed, code_files_processed).
    """
    docs = 0
    code = 0

    # Load provider config so markdown extraction uses the right LLM
    config_path = root / ".memory" / "config.json"
    provider_config: dict = {}
    if config_path.exists():
        try:
            provider_config = json.loads(config_path.read_text())
        except Exception:
            pass

    async with get_db() as db:
        for path in iter_project_files(root):
            is_doc = path.suffix in _DOC_EXTENSIONS
            is_config = path.name in _CONFIG_FILENAMES
            is_code = path.suffix in _CODE_EXTENSIONS

            if not (is_doc or is_config or is_code):
                continue

            await process_file(path, root, project_id, db, provider_config)
            await _upsert_directory_hierarchy(db, root, path, project_id)

            if is_doc or is_config:
                docs += 1
            else:
                code += 1

        # Post-ingest: promote causal edges from observed structural patterns
        await promote_high_fan_in_to_drives(db, project_id)
        await promote_test_assertions(db, project_id)
        await promote_mutation_drives(db, project_id)

        cycles = await detect_causal_cycles(db, project_id)
        for cycle in cycles:
            log.warning("[ingester] causal cycle detected: %s", cycle)

    return docs, code
