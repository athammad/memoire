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

from .sdk import store_document, store_entity, store_relationship, prune_extracted_edges, _is_test_path

log = logging.getLogger("[processor]")

_MAX_BYTES = 50_000
_MAX_MD_CHARS_FOR_LLM = 8_000  # keep API cost low

# Patterns for runtime side-effect detection — each key is a category
_SIDE_EFFECT_PATTERNS: dict[str, list[str]] = {
    "network": [
        r"\brequests\.", r"\bhttpx\.", r"\baiohttp\.", r"\burllib\b",
        r"\bhttp\.client\b", r"\bwebsockets\b", r"\bgrpc\b",
    ],
    "file_io": [
        r"\bopen\s*\(", r"\.write_text\s*\(", r"\.read_text\s*\(",
        r"\.write\s*\(", r"\.writelines\s*\(",
    ],
    "subprocess": [
        r"\bsubprocess\.", r"\bos\.system\s*\(", r"\bos\.popen\s*\(",
        r"\bshutil\.(copy|move|rmtree)\b",
    ],
    "database": [
        r"\bsqlite3\.", r"\bpsycopg\b", r"\baiomysql\b",
        r"\bsqlalchemy\b", r"\.execute\s*\(", r"\.commit\s*\(",
    ],
    "cache": [
        r"\bredis\.", r"\bmemcache\b", r"\bdiskcache\b",
    ],
}

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
    "You are building a causal knowledge graph for a software project.\n"
    "Analyze this document and extract causal relationships — not just what things are, "
    "but what causes what.\n\n"
    "Use only these relation types:\n"
    "  SPECIFIES  — this doc defines the intent/contract that a code file must implement\n"
    "  IMPLEMENTS — a code file is the concrete realization of an idea in this doc\n"
    "  DRIVES     — changing the source will force changes in the target\n"
    "  DOCUMENTS  — this doc describes the behavior of a code file (effect, not cause)\n"
    "  RELATES_TO — non-causal association (fallback only)\n\n"
    "Return JSON only, no explanation:\n"
    '{"entities":[{"name":"...","type":"concept|module|service|decision"}],'
    '"relationships":[{"source":"...","relation":"SPECIFIES|IMPLEMENTS|DRIVES|DOCUMENTS|RELATES_TO",'
    '"target":"...","rationale":"one sentence why this is causal"}]}\n\n'
)


# ---------------------------------------------------------------------------
# Side-effect and mutation analysis
# ---------------------------------------------------------------------------

def _detect_side_effects(content: str) -> list[str]:
    """Return detected runtime side-effect categories present in the file."""
    return [
        category
        for category, patterns in _SIDE_EFFECT_PATTERNS.items()
        if any(re.search(p, content) for p in patterns)
    ]


def _extract_state_mutations(content: str) -> list[str]:
    """
    Return attribute names written via self.attr = ... in the file.

    These are potential mutation sources: other files that import this class
    and read these attributes will break silently when the write logic changes.
    """
    return sorted({
        m.group(1)
        for m in re.finditer(r"\bself\.(\w+)\s*=(?!=)", content, re.MULTILINE)
        if not m.group(1).startswith("_")  # skip private attrs — less likely to be read externally
    })


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
    provider_config: dict | None = None,
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

    # --- Side-effect and mutation analysis ---
    side_effects: list[str] = []
    writes_state: list[str] = []
    if suffix == ".py":
        side_effects = _detect_side_effects(content)
        writes_state = _extract_state_mutations(content)
    elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
        side_effects = _detect_side_effects_ts(content)
        writes_state = _extract_state_mutations_ts(content)
    elif suffix == ".go":
        side_effects = _detect_side_effects_go(content)
    elif suffix == ".rs":
        side_effects = _detect_side_effects_rust(content)
        writes_state = _extract_state_mutations_rust(content)
    elif suffix == ".java":
        side_effects = _detect_side_effects_java(content)
        writes_state = _extract_state_mutations_java(content)
    elif suffix == ".rb":
        side_effects = _detect_side_effects_ruby(content)
        writes_state = _extract_state_mutations_ruby(content)
    elif suffix in {".c", ".cpp", ".h", ".cc", ".cxx"}:
        side_effects = _detect_side_effects_c(content)

    # --- Document representation ---
    if suffix in _DOC_EXTENSIONS or path.name in _CONFIG_FILENAMES:
        await store_document(db, project_id, title=rel, content=content,
                             tags=[suffix.lstrip(".") or path.name])
    elif suffix in _CODE_EXTENSIONS:
        lang = "Python" if suffix == ".py" else suffix.lstrip(".")
        parts = [f"{lang} file: {rel}"]
        if side_effects:
            parts.append(f"side-effects: {', '.join(side_effects)}")
        await store_entity(
            db, project_id, name=rel, entity_type="file",
            summary=" | ".join(parts),
            side_effects=side_effects,
            writes_state=writes_state,
        )

    # --- Graph representation ---
    relationships = []

    if suffix == ".py":
        relationships = _extract_python(content, rel)
    elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
        relationships = _extract_typescript(content, rel)
    elif suffix == ".go":
        relationships = _extract_go(content, rel)
    elif suffix == ".rs":
        relationships = _extract_rust(content, rel)
    elif suffix == ".java":
        relationships = _extract_java(content, rel)
    elif suffix == ".rb":
        relationships = _extract_ruby(content, rel)
    elif suffix in {".c", ".cpp", ".h", ".cc", ".cxx"}:
        relationships = _extract_c(content, rel)
    elif suffix in _DOC_EXTENSIONS:
        relationships = await _extract_markdown(content, rel, provider_config)

    new_triples: list[tuple[str, str, str]] = []
    for rel_item in relationships:
        if rel_item["source"] and rel_item["target"]:
            triple = (rel_item["source"], rel_item["relation"], rel_item["target"])
            await store_relationship(
                db,
                project_id=project_id,
                source=triple[0],
                relation=triple[1],
                target=triple[2],
                rationale=rel_item.get("rationale", ""),
                cost=rel_item.get("cost", "normal"),
                extracted_from=rel,
            )
            new_triples.append(triple)

    # Remove edges that were previously extracted from this file but no longer exist in it
    pruned = await prune_extracted_edges(db, project_id, rel, new_triples)
    if pruned:
        log.debug("[processor] pruned %d stale edges from %s", pruned, rel)

    # Persist hash so next run can skip unchanged files
    await _save_hash(db, project_id, rel, current_hash)


# ---------------------------------------------------------------------------
# Pattern-based extractors (code)
# ---------------------------------------------------------------------------

def _extract_python(content: str, file_path: str) -> list[dict]:
    """Extract import dependencies, class inheritance, and test assertion edges from Python source."""
    rels = []
    is_test = _is_test_path(file_path)

    for m in re.finditer(r"^import\s+([\w.]+)", content, re.MULTILINE):
        tgt = m.group(1)
        rels.append({"source": file_path, "relation": "IMPORTS", "target": tgt})
        if is_test:
            rels.append({
                "source": file_path,
                "relation": "ASSERTS_ON",
                "target": tgt,
                "rationale": f"Test file imports {tgt} — assertions here will fail if {tgt} changes",
                "cost": "high",
            })

    for m in re.finditer(r"^from\s+([\w.]+)\s+import", content, re.MULTILINE):
        tgt = m.group(1)
        rels.append({"source": file_path, "relation": "IMPORTS", "target": tgt})
        if is_test:
            rels.append({
                "source": file_path,
                "relation": "ASSERTS_ON",
                "target": tgt,
                "rationale": f"Test file imports {tgt} — assertions here will fail if {tgt} changes",
                "cost": "high",
            })

    for m in re.finditer(r"^class\s+(\w+)\s*\(([^)]+)\)", content, re.MULTILINE):
        cls = m.group(1)
        for parent in (p.strip() for p in m.group(2).split(",")):
            if parent and parent != "object":
                rels.append({"source": cls, "relation": "INHERITS", "target": parent})

    return rels


_SIDE_EFFECT_PATTERNS_TS: dict[str, list[str]] = {
    "network": [
        r"\bfetch\s*\(", r"\baxios\.", r"\bXMLHttpRequest\b",
        r"\bWebSocket\b", r"\bEventSource\b",
    ],
    "file_io": [
        r"\bfs\.\w*(write|read|append|unlink|mkdir)",
        r"\bfs\.promises\.", r"\bfsPromises\.",
        r"\bDeno\.(write|read|open|create)\b",
    ],
    "subprocess": [
        r"\bexec\s*\(", r"\bspawn\s*\(", r"\bchild_process\b",
        r"\bDeno\.run\b",
    ],
    "database": [
        r"\bprisma\.", r"\bmongoose\.", r"\bsequelize\b", r"\bknex\b",
        r"\bpg\b", r"\bmysql\b", r"\.query\s*\(", r"\.execute\s*\(",
    ],
    "cache": [
        r"\bredis\b", r"\bioredis\b",
    ],
}

_SIDE_EFFECT_PATTERNS_GO: dict[str, list[str]] = {
    "network": [
        r'"net/http"', r'\bhttp\.Get\b', r'\bhttp\.Post\b',
        r'\bhttp\.NewRequest\b', r'\bnet\.Dial\b',
    ],
    "file_io": [
        r'\bos\.(Create|Open|Write|Remove|Rename)\b',
        r'\bioutil\.(Write|Read)File\b', r'\bos\.WriteFile\b',
    ],
    "subprocess": [
        r'\bexec\.Command\b', r'\bos\.StartProcess\b',
    ],
    "database": [
        r'"database/sql"', r'\bsql\.Open\b', r'\.Exec\s*\(', r'\.Query\s*\(',
    ],
}


def _detect_side_effects_ts(content: str) -> list[str]:
    """Return detected runtime side-effect categories present in a TypeScript/JS file."""
    return [
        cat for cat, patterns in _SIDE_EFFECT_PATTERNS_TS.items()
        if any(re.search(p, content) for p in patterns)
    ]


def _detect_side_effects_go(content: str) -> list[str]:
    """Return detected runtime side-effect categories present in a Go file."""
    return [
        cat for cat, patterns in _SIDE_EFFECT_PATTERNS_GO.items()
        if any(re.search(p, content) for p in patterns)
    ]


def _extract_state_mutations_ts(content: str) -> list[str]:
    """Return attribute names written via this.attr = ... in TypeScript/JS."""
    return sorted({
        m.group(1)
        for m in re.finditer(r"\bthis\.(\w+)\s*=(?!=)", content, re.MULTILINE)
        if not m.group(1).startswith("_")
    })


def _extract_typescript(content: str, file_path: str) -> list[dict]:
    """Extract imports, inheritance, and test assertion edges from TypeScript/JS source."""
    rels = []
    is_test = _is_test_path(file_path)

    for m in re.finditer(
        r"""^import\s+.*?\s+from\s+['"]([^'"]+)['"]""", content, re.MULTILINE
    ):
        tgt = m.group(1)
        rels.append({"source": file_path, "relation": "IMPORTS", "target": tgt})
        if is_test:
            rels.append({
                "source": file_path,
                "relation": "ASSERTS_ON",
                "target": tgt,
                "rationale": f"Test file imports {tgt} — assertions here will fail if {tgt} changes",
                "cost": "high",
            })

    for m in re.finditer(r"class\s+(\w+)\s+extends\s+(\w+)", content):
        rels.append({"source": m.group(1), "relation": "INHERITS", "target": m.group(2)})

    for m in re.finditer(r"class\s+(\w+)\s+implements\s+([\w,\s]+)", content):
        cls = m.group(1)
        for iface in (i.strip() for i in m.group(2).split(",")):
            if iface:
                rels.append({"source": cls, "relation": "IMPLEMENTS", "target": iface})

    return rels


def _extract_go(content: str, file_path: str) -> list[dict]:
    """Extract import dependencies and test assertion edges from Go source."""
    rels = []
    is_test = file_path.endswith("_test.go")

    # Single import
    for m in re.finditer(r'^import\s+"([^"]+)"', content, re.MULTILINE):
        tgt = m.group(1)
        rels.append({"source": file_path, "relation": "IMPORTS", "target": tgt})
        if is_test:
            rels.append({
                "source": file_path,
                "relation": "ASSERTS_ON",
                "target": tgt,
                "rationale": f"Go test file imports {tgt} — test will fail if {tgt} changes",
                "cost": "high",
            })

    # Import block
    block = re.search(r"import\s*\(([^)]+)\)", content, re.DOTALL)
    if block:
        for m in re.finditer(r'"([^"]+)"', block.group(1)):
            tgt = m.group(1)
            rels.append({"source": file_path, "relation": "IMPORTS", "target": tgt})
            if is_test:
                rels.append({
                    "source": file_path,
                    "relation": "ASSERTS_ON",
                    "target": tgt,
                    "rationale": f"Go test file imports {tgt} — test will fail if {tgt} changes",
                    "cost": "high",
                })

    return rels


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------

_SIDE_EFFECT_PATTERNS_RUST: dict[str, list[str]] = {
    "network": [
        r'\breqwest\b', r'\bhyper\b', r'\btokio::net\b',
        r'\bstd::net\b', r'\bTcpStream\b', r'\bUdpSocket\b',
    ],
    "file_io": [
        r'\bstd::fs\b', r'\bFile::open\b', r'\bFile::create\b',
        r'\bfs::write\b', r'\bfs::read\b', r'\bBufWriter\b',
    ],
    "subprocess": [
        r'\bstd::process::Command\b', r'\bCommand::new\b',
    ],
    "database": [
        r'\bsqlx\b', r'\bdiesel\b', r'\brusqlite\b', r'\btokio_postgres\b',
    ],
    "cache": [
        r'\bredis\b', r'\bmobc\b',
    ],
}


def _detect_side_effects_rust(content: str) -> list[str]:
    """Return detected runtime side-effect categories present in a Rust file."""
    return [
        cat for cat, patterns in _SIDE_EFFECT_PATTERNS_RUST.items()
        if any(re.search(p, content) for p in patterns)
    ]


def _extract_state_mutations_rust(content: str) -> list[str]:
    """Return field names written via self.field = ... in Rust."""
    return sorted({
        m.group(1)
        for m in re.finditer(r"\bself\.(\w+)\s*=(?!=)", content, re.MULTILINE)
        if not m.group(1).startswith("_")
    })


def _extract_rust(content: str, file_path: str) -> list[dict]:
    """Extract use dependencies, trait implementations, and test assertion edges from Rust source."""
    rels = []
    is_test = _is_test_path(file_path)

    # use statements: `use std::io::Write;` or `use crate::module;`
    for m in re.finditer(r"^use\s+([\w:]+)", content, re.MULTILINE):
        tgt = m.group(1).split("::")[0]  # top-level crate/module
        rels.append({"source": file_path, "relation": "IMPORTS", "target": tgt})
        if is_test:
            rels.append({
                "source": file_path,
                "relation": "ASSERTS_ON",
                "target": tgt,
                "rationale": f"Rust test file uses {tgt} — tests will fail if {tgt} changes",
                "cost": "high",
            })

    # trait implementations: `impl Trait for Type`
    for m in re.finditer(r"\bimpl\s+(\w+)\s+for\s+(\w+)", content):
        rels.append({"source": m.group(2), "relation": "IMPLEMENTS", "target": m.group(1)})

    return rels


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------

_SIDE_EFFECT_PATTERNS_JAVA: dict[str, list[str]] = {
    "network": [
        r'\bjava\.net\b', r'\bHttpClient\b', r'\bHttpURLConnection\b',
        r'\bOkHttpClient\b', r'\bRestTemplate\b', r'\bWebClient\b',
    ],
    "file_io": [
        r'\bjava\.io\b', r'\bFiles\.\b', r'\bFileWriter\b',
        r'\bFileInputStream\b', r'\bBufferedWriter\b', r'\bPrintWriter\b',
    ],
    "subprocess": [
        r'\bRuntime\.exec\b', r'\bProcessBuilder\b',
    ],
    "database": [
        r'\bjava\.sql\b', r'\bJdbcTemplate\b', r'\bEntityManager\b',
        r'\bHibernate\b', r'\bDataSource\b',
    ],
    "cache": [
        r'\bRedisTemplate\b', r'\bCacheManager\b', r'\bJedis\b',
    ],
}


def _detect_side_effects_java(content: str) -> list[str]:
    """Return detected runtime side-effect categories present in a Java file."""
    return [
        cat for cat, patterns in _SIDE_EFFECT_PATTERNS_JAVA.items()
        if any(re.search(p, content) for p in patterns)
    ]


def _extract_state_mutations_java(content: str) -> list[str]:
    """Return field names written via this.field = ... in Java."""
    return sorted({
        m.group(1)
        for m in re.finditer(r"\bthis\.(\w+)\s*=(?!=)", content, re.MULTILINE)
        if not m.group(1).startswith("_")
    })


def _extract_java(content: str, file_path: str) -> list[dict]:
    """Extract import dependencies, inheritance, and test assertion edges from Java source."""
    rels = []
    is_test = _is_test_path(file_path)

    for m in re.finditer(r"^import\s+([\w.]+);", content, re.MULTILINE):
        tgt = m.group(1).rsplit(".", 1)[0]  # package path without the class
        rels.append({"source": file_path, "relation": "IMPORTS", "target": tgt})
        if is_test:
            rels.append({
                "source": file_path,
                "relation": "ASSERTS_ON",
                "target": tgt,
                "rationale": f"Java test imports {tgt} — test will fail if {tgt} changes",
                "cost": "high",
            })

    # class A extends B
    for m in re.finditer(r"\bclass\s+(\w+)\s+extends\s+(\w+)", content):
        rels.append({"source": m.group(1), "relation": "INHERITS", "target": m.group(2)})

    # class A implements B, C
    for m in re.finditer(r"\bclass\s+(\w+)[\w\s<>]*\bimplements\s+([\w,\s<>]+?)(?:\{|extends)", content):
        cls = m.group(1)
        for iface in (i.strip() for i in re.split(r",", m.group(2))):
            iface = iface.strip().split("<")[0]  # strip generics
            if iface:
                rels.append({"source": cls, "relation": "IMPLEMENTS", "target": iface})

    return rels


# ---------------------------------------------------------------------------
# Ruby
# ---------------------------------------------------------------------------

_SIDE_EFFECT_PATTERNS_RUBY: dict[str, list[str]] = {
    "network": [
        r'\bNet::HTTP\b', r'\bhttparty\b', r'\bfaraday\b',
        r'\brestclient\b', r'\bopen-uri\b', r'\bURI\.open\b',
    ],
    "file_io": [
        r'\bFile\.(open|write|read|delete|rename)\b',
        r'\bIO\.(read|write)\b', r'\bFileUtils\b',
    ],
    "subprocess": [
        r'\bOpen3\b', r'\bsystem\s*\(', r'\bspawn\s*\(', r'`[^`]+`',
    ],
    "database": [
        r'\bActiveRecord\b', r'\bSequelize\b', r'\bSQLite3\b',
        r'\.where\s*\(', r'\.find\s*\(', r'\.save\b',
    ],
    "cache": [
        r'\bRedis\b', r'\bDalli\b', r'\bMemcache\b',
    ],
}


def _detect_side_effects_ruby(content: str) -> list[str]:
    """Return detected runtime side-effect categories present in a Ruby file."""
    return [
        cat for cat, patterns in _SIDE_EFFECT_PATTERNS_RUBY.items()
        if any(re.search(p, content) for p in patterns)
    ]


def _extract_state_mutations_ruby(content: str) -> list[str]:
    """Return instance variable names written via @attr = ... in Ruby."""
    return sorted({
        m.group(1)
        for m in re.finditer(r"@(\w+)\s*=(?!=)", content, re.MULTILINE)
        if not m.group(1).startswith("_")
    })


def _extract_ruby(content: str, file_path: str) -> list[dict]:
    """Extract require dependencies, inheritance, and test assertion edges from Ruby source."""
    rels = []
    is_test = _is_test_path(file_path)

    # require / require_relative
    for m in re.finditer(r"""^\s*require(?:_relative)?\s+['"]([^'"]+)['"]""", content, re.MULTILINE):
        tgt = m.group(1)
        rels.append({"source": file_path, "relation": "IMPORTS", "target": tgt})
        if is_test:
            rels.append({
                "source": file_path,
                "relation": "ASSERTS_ON",
                "target": tgt,
                "rationale": f"Ruby test requires {tgt} — test will fail if {tgt} changes",
                "cost": "high",
            })

    # class Dog < Animal
    for m in re.finditer(r"^class\s+(\w+)\s*<\s*([\w:]+)", content, re.MULTILINE):
        rels.append({"source": m.group(1), "relation": "INHERITS", "target": m.group(2)})

    return rels


# ---------------------------------------------------------------------------
# C / C++
# ---------------------------------------------------------------------------

_SIDE_EFFECT_PATTERNS_C: dict[str, list[str]] = {
    "network": [
        r'\bsocket\s*\(', r'\bconnect\s*\(', r'\bcurl_easy_\w+\s*\(',
        r'\bgetaddrinfo\s*\(', r'\bsend\s*\(', r'\brecv\s*\(',
    ],
    "file_io": [
        r'\bfopen\s*\(', r'\bfwrite\s*\(', r'\bfread\s*\(',
        r'\bfclose\s*\(', r'\bremove\s*\(', r'\bunlink\s*\(',
        r'\bstd::ofstream\b', r'\bstd::ifstream\b',
    ],
    "subprocess": [
        r'\bsystem\s*\(', r'\bpopen\s*\(', r'\bexecv\w*\s*\(',
        r'\bfork\s*\(',
    ],
    "database": [
        r'\bsqlite3_\w+\s*\(', r'\bmysql_\w+\s*\(', r'\bPQexec\b',
    ],
}


def _detect_side_effects_c(content: str) -> list[str]:
    """Return detected runtime side-effect categories present in a C/C++ file."""
    return [
        cat for cat, patterns in _SIDE_EFFECT_PATTERNS_C.items()
        if any(re.search(p, content) for p in patterns)
    ]


def _extract_c(content: str, file_path: str) -> list[dict]:
    """Extract #include dependencies and test assertion edges from C/C++ source."""
    rels = []
    is_test = _is_test_path(file_path)

    # #include <header> and #include "header"
    for m in re.finditer(r'^#include\s+[<"]([^>"]+)[>"]', content, re.MULTILINE):
        tgt = m.group(1)
        rels.append({"source": file_path, "relation": "IMPORTS", "target": tgt})
        if is_test:
            rels.append({
                "source": file_path,
                "relation": "ASSERTS_ON",
                "target": tgt,
                "rationale": f"C/C++ test includes {tgt} — test will fail if {tgt} changes",
                "cost": "high",
            })

    # C++ class inheritance: class Dog : public Animal
    for m in re.finditer(
        r"\bclass\s+(\w+)\s*:[^{]*?(?:public|protected|private)\s+(\w+)", content
    ):
        rels.append({"source": m.group(1), "relation": "INHERITS", "target": m.group(2)})

    return rels


# ---------------------------------------------------------------------------
# LLM-based extractor (markdown)
# ---------------------------------------------------------------------------

def _strip_fences(raw: str) -> str:
    """Strip markdown code fences from an LLM response if present."""
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        return "\n".join(lines)
    return raw


def _parse_relationships(raw: str) -> list[dict]:
    """Parse the JSON relationships list from an LLM response."""
    data = json.loads(_strip_fences(raw))
    return [
        {
            "source": r.get("source", ""),
            "relation": r.get("relation", "RELATES_TO"),
            "target": r.get("target", ""),
            "rationale": r.get("rationale", ""),
        }
        for r in data.get("relationships", [])
    ]


async def _call_llm(prompt: str, config: dict) -> str:
    """
    Dispatch a prompt to the configured LLM provider and return the raw text.

    Supported providers (config["llm"]):
      claude    — uses the `claude --print` CLI (no API key needed)
      anthropic — uses the Anthropic Messages API (ANTHROPIC_API_KEY)
      openai    — uses the OpenAI Chat Completions API (OPENAI_API_KEY)
      gemini    — uses the Google Generative Language API (GEMINI_API_KEY)
      ollama    — uses a local Ollama instance (http://localhost:11434)
    """
    import os
    import httpx as _httpx

    llm = config.get("llm", "claude")
    model = config.get("llm_model", "")

    if llm == "claude":
        if not shutil.which("claude"):
            return ""
        result = subprocess.run(
            ["claude", "--print"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.stdout.strip()

    if llm == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return ""
        async with _httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model or "claude-sonnet-4-6",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]

    if llm == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return ""
        async with _httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
                json={
                    "model": model or "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    if llm == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            return ""
        gem_model = model or "gemini-1.5-flash"
        async with _httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{gem_model}:generateContent",
                params={"key": api_key},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    if llm == "ollama":
        base_url = config.get("ollama_url", "http://localhost:11434")
        async with _httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/api/chat",
                json={
                    "model": model or "llama3",
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["message"]["content"]

    return ""


async def _extract_markdown(
    content: str,
    file_path: str,
    provider_config: dict | None = None,
) -> list[dict]:
    """
    Extract causal relationships from markdown using the configured LLM provider.

    Defaults to the `claude` CLI when no provider_config is given.
    Skips silently if the provider is unavailable or the API key is missing.
    """
    config = provider_config or {"llm": "claude"}
    prompt = _MD_PROMPT + content[:_MAX_MD_CHARS_FOR_LLM]
    try:
        raw = await _call_llm(prompt, config)
        if not raw:
            return []
        return _parse_relationships(raw)
    except Exception as exc:
        log.warning("[processor] markdown extraction failed for %s: %s", file_path, exc)
        return []
