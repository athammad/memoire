"""
Dual-representation processor.

Every file that enters the system is processed into two forms:
  1. Document — full text stored for full-text search
  2. Graph    — entities and relationships extracted for graph queries

This module is the single place that does both. It is called by:
  - memoire ingest  (initial pass over existing files)
  - daemon          (on file save via file watcher)
  - daemon          (on Claude file read/edit via hooks)
"""

import hashlib
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from surrealdb.connections.async_ws import AsyncWsSurrealConnection

from .sdk import store_document, store_entity, store_relationship

log = logging.getLogger("[processor]")

_MAX_BYTES = 50_000
_MAX_MD_CHARS_FOR_LLM = 8_000  # keep API cost low

_CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
    ".cpp", ".c", ".h", ".rb", ".php", ".swift", ".kt", ".cs",
}
_DOC_EXTENSIONS = {".md", ".rst", ".txt"}
_CONFIG_FILENAMES = {
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "requirements.txt", "setup.py", "setup.cfg", "tsconfig.json",
}

_MD_PROMPT = (
    "Extract entities and relationships from this document. "
    "Return JSON only, no explanation:\n"
    '{"entities":[{"name":"...","type":"..."}],'
    '"relationships":[{"source":"...","relation":"...","target":"..."}]}\n\n'
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _hash(content: str) -> str:
    return hashlib.sha1(content.encode(errors="ignore")).hexdigest()


async def _get_stored_hash(
    db: AsyncWsSurrealConnection, project_id: str, rel: str
) -> str | None:
    """Return the content hash stored on the entity, or None if not set."""
    rows = await db.query(
        "SELECT content_hash FROM entities WHERE project_id = $pid AND name = $name LIMIT 1",
        {"pid": project_id, "name": rel},
    )
    if isinstance(rows, list) and rows:
        return rows[0].get("content_hash")
    return None


async def _save_hash(
    db: AsyncWsSurrealConnection, project_id: str, rel: str, content_hash: str
) -> None:
    await db.query(
        """
        UPDATE entities SET content_hash = $hash
        WHERE project_id = $pid AND name = $name
        """,
        {"hash": content_hash, "pid": project_id, "name": rel},
    )


async def process_file(
    path: Path,
    root: Path,
    project_id: str,
    db: AsyncWsSurrealConnection,
) -> None:
    """
    Process a single file into its document and graph representations.

    Skips processing if the file content has not changed since last run,
    avoiding unnecessary LLM calls on unchanged markdown files.
    """
    if not path.is_file():
        return
    if path.stat().st_size > _MAX_BYTES:
        rel = str(path.relative_to(root))
        await store_entity(db, project_id, rel, "file", f"Large file: {rel}")
        return

    try:
        content = path.read_text(errors="ignore")
    except Exception as exc:
        log.warning("[processor] could not read %s: %s", path, exc)
        return

    rel = str(path.relative_to(root))

    # Skip if content unchanged — avoids redundant LLM calls
    current_hash = _hash(content)
    if await _get_stored_hash(db, project_id, rel) == current_hash:
        log.debug("[processor] unchanged, skipping: %s", rel)
        return
    suffix = path.suffix.lower()

    # --- Document representation ---
    if suffix in _DOC_EXTENSIONS or path.name in _CONFIG_FILENAMES:
        await store_document(db, project_id, title=rel, content=content,
                             tags=[suffix.lstrip(".") or path.name])
    elif suffix in _CODE_EXTENSIONS:
        await store_entity(db, project_id, name=rel, entity_type="file",
                           summary=f"{'Python' if suffix == '.py' else suffix.lstrip('.')} file: {rel}")

    # --- Graph representation ---
    relationships = []

    if suffix == ".py":
        relationships = _extract_python(content, rel)
    elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
        relationships = _extract_typescript(content, rel)
    elif suffix in {".go"}:
        relationships = _extract_go(content, rel)
    elif suffix in _DOC_EXTENSIONS:
        relationships = await _extract_markdown(content, rel)

    for rel_item in relationships:
        if rel_item["source"] and rel_item["target"]:
            await store_relationship(
                db,
                project_id=project_id,
                source=rel_item["source"],
                relation=rel_item["relation"],
                target=rel_item["target"],
            )

    # Persist hash so next run can skip unchanged files
    await _save_hash(db, project_id, rel, current_hash)


# ---------------------------------------------------------------------------
# Pattern-based extractors (code)
# ---------------------------------------------------------------------------

def _extract_python(content: str, file_path: str) -> list[dict]:
    """Extract import dependencies and class inheritance from Python source."""
    rels = []

    for m in re.finditer(r"^import\s+([\w.]+)", content, re.MULTILINE):
        rels.append({"source": file_path, "relation": "IMPORTS", "target": m.group(1)})

    for m in re.finditer(r"^from\s+([\w.]+)\s+import", content, re.MULTILINE):
        rels.append({"source": file_path, "relation": "IMPORTS", "target": m.group(1)})

    for m in re.finditer(r"^class\s+(\w+)\s*\(([^)]+)\)", content, re.MULTILINE):
        cls = m.group(1)
        for parent in (p.strip() for p in m.group(2).split(",")):
            if parent and parent != "object":
                rels.append({"source": cls, "relation": "INHERITS", "target": parent})

    return rels


def _extract_typescript(content: str, file_path: str) -> list[dict]:
    """Extract import dependencies and class inheritance from TypeScript/JS source."""
    rels = []

    for m in re.finditer(
        r"""^import\s+.*?\s+from\s+['"]([^'"]+)['"]""", content, re.MULTILINE
    ):
        rels.append({"source": file_path, "relation": "IMPORTS", "target": m.group(1)})

    for m in re.finditer(r"class\s+(\w+)\s+extends\s+(\w+)", content):
        rels.append({"source": m.group(1), "relation": "INHERITS", "target": m.group(2)})

    for m in re.finditer(r"class\s+(\w+)\s+implements\s+([\w,\s]+)", content):
        cls = m.group(1)
        for iface in (i.strip() for i in m.group(2).split(",")):
            if iface:
                rels.append({"source": cls, "relation": "IMPLEMENTS", "target": iface})

    return rels


def _extract_go(content: str, file_path: str) -> list[dict]:
    """Extract import dependencies from Go source."""
    rels = []

    # Single import
    for m in re.finditer(r'^import\s+"([^"]+)"', content, re.MULTILINE):
        rels.append({"source": file_path, "relation": "IMPORTS", "target": m.group(1)})

    # Import block
    block = re.search(r"import\s*\(([^)]+)\)", content, re.DOTALL)
    if block:
        for m in re.finditer(r'"([^"]+)"', block.group(1)):
            rels.append({"source": file_path, "relation": "IMPORTS", "target": m.group(1)})

    return rels


# ---------------------------------------------------------------------------
# LLM-based extractor (markdown)
# ---------------------------------------------------------------------------

async def _extract_markdown(content: str, file_path: str) -> list[dict]:
    """
    Extract entities and relationships from markdown using the claude CLI.

    Uses the same authentication as Claude Code (VS Code OAuth) — no API key needed.
    Skips silently if the claude CLI is not available.
    """
    if not shutil.which("claude"):
        return []

    prompt = _MD_PROMPT + content[:_MAX_MD_CHARS_FOR_LLM]
    try:
        result = subprocess.run(
            ["claude", "--print"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw = result.stdout.strip()
        if not raw:
            return []

        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.splitlines()
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            raw = "\n".join(lines)

        data = json.loads(raw)
        return [
            {
                "source": r.get("source", ""),
                "relation": r.get("relation", "RELATES_TO"),
                "target": r.get("target", ""),
            }
            for r in data.get("relationships", [])
        ]
    except Exception as exc:
        log.warning("[processor] markdown extraction failed for %s: %s", file_path, exc)
        return []
