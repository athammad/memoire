"""Project file ingestion — quick scan and deep ingest."""

from pathlib import Path

from .db import get_db
from .processor import process_file
from .sdk import store_entity

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
            count += 1
    return count


async def deep_ingest(root: Path, project_id: str) -> tuple[int, int]:
    """
    Full ingestion pass — processes every file into both document and graph form.

    Delegates to processor.process_file which handles:
      - document storage (full text, searchable)
      - graph extraction (imports, inheritance for code; Claude API for markdown)

    Returns (docs_processed, code_files_processed).
    """
    docs = 0
    code = 0

    async with get_db() as db:
        for path in iter_project_files(root):
            is_doc = path.suffix in _DOC_EXTENSIONS
            is_config = path.name in _CONFIG_FILENAMES
            is_code = path.suffix in _CODE_EXTENSIONS

            if not (is_doc or is_config or is_code):
                continue

            await process_file(path, root, project_id, db)

            if is_doc or is_config:
                docs += 1
            else:
                code += 1

    return docs, code
