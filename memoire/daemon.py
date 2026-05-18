"""Background daemon — receives hook events and watches the file system."""

import asyncio
import json
import logging
import signal
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .db import DAEMON_PORT, get_db, ensure_schema, load_project_config
from .processor import process_file
from .sdk import (
    store_event,
    touch_entity,
    delete_entity,
    promote_high_fan_in_to_drives,
    promote_test_assertions,
    promote_mutation_drives,
    detect_causal_cycles,
)

log = logging.getLogger("[daemon]")

_WATCH_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
    ".md", ".yaml", ".yml", ".toml", ".sql", ".sh",
}

# Hook tools that mean a file was read or written — trigger full processing
_FILE_READ_TOOLS = {"Read", "NotebookEdit"}
_FILE_WRITE_TOOLS = {"Edit", "Write"}
_FILE_TOOLS = _FILE_READ_TOOLS | _FILE_WRITE_TOOLS

# Window for inferring temporal causality between sequential file edits
_CAUSAL_WINDOW_SECONDS = 300  # 5 minutes


class _ProjectFileHandler(FileSystemEventHandler):
    """Queues file-change events into the async processing loop."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self._queue = queue
        self._loop = loop

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if path.suffix in _WATCH_EXTENSIONS and ".memory" not in path.parts:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait,
                {"kind": "file_changed", "path": str(path)},
            )

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if path.suffix in _WATCH_EXTENSIONS and ".memory" not in path.parts:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait,
                {"kind": "file_deleted", "path": str(path)},
            )


class _HookServer(BaseHTTPRequestHandler):
    """Minimal HTTP server that accepts Claude Code hook events on POST /hook."""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            event = json.loads(body)
            self.server.loop.call_soon_threadsafe(self.server.queue.put_nowait, event)
            self.send_response(200)
        except Exception:
            self.send_response(400)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


async def _process_events(
    queue: asyncio.Queue,
    project_id: str,
    project_root: Path,
    provider_config: dict[str, Any] | None = None,
) -> None:
    """Consume events from the queue and persist them to SurrealDB."""
    # Recent write-tool edits: deque of (timestamp, rel_path) for causal inference
    edit_sequence: deque[tuple[float, str]] = deque(maxlen=20)
    # Count of file changes since last promotion run
    changes_since_promotion = 0
    _PROMOTION_INTERVAL = 10  # re-run promotions every N file changes

    async with get_db() as db:
        await ensure_schema(db)
        log.info("[daemon] ready — project_id=%s", project_id)

        while True:
            event = await queue.get()

            try:
                kind = event.get("kind")

                if kind == "file_changed":
                    path = Path(event["path"])
                    if path.is_file():
                        await process_file(path, project_root, project_id, db, provider_config)
                        changes_since_promotion += 1
                        log.debug("[daemon] processed changed file: %s", path)

                elif kind == "file_deleted":
                    path = Path(event["path"])
                    try:
                        rel = str(path.relative_to(project_root))
                        await delete_entity(db, project_id, rel)
                        changes_since_promotion += 1
                        log.info("[daemon] removed deleted file from graph: %s", rel)
                    except ValueError:
                        pass  # path not under project root

                else:
                    tool_name = event.get("tool_name", "")
                    tool_input = event.get("tool_input", {})

                    if tool_name in _FILE_TOOLS:
                        file_path = tool_input.get("file_path", "")
                        if file_path:
                            path = Path(file_path)
                            if path.is_file() and project_root in path.parents:
                                rel = str(path.relative_to(project_root))
                                await process_file(path, project_root, project_id, db, provider_config)
                                await touch_entity(db, project_id, rel)
                                changes_since_promotion += 1

                                # Temporal causality: infer DRIVES from edit sequences
                                if tool_name in _FILE_WRITE_TOOLS:
                                    now = time.monotonic()
                                    cutoff = now - _CAUSAL_WINDOW_SECONDS
                                    for ts, prev_rel in edit_sequence:
                                        if ts >= cutoff and prev_rel != rel:
                                            await store_relationship(
                                                db, project_id,
                                                source=prev_rel,
                                                relation="DRIVES",
                                                target=rel,
                                                rationale=(
                                                    "Inferred from sequential edits within "
                                                    f"{_CAUSAL_WINDOW_SECONDS // 60} minutes"
                                                ),
                                            )
                                    edit_sequence.append((now, rel))

                                log.debug("[daemon] processed hook file: %s", path)

                    elif tool_name == "Bash":
                        command = (tool_input.get("command") or "").strip()
                        interesting = ("git ", "pip ", "npm ", "docker ", "pytest", "python ")
                        if command and any(command.startswith(p) for p in interesting):
                            await store_event(
                                db,
                                project_id=project_id,
                                summary=f"Ran: {command[:200]}",
                                importance=0.5,
                                entities=[],
                            )

                # Re-run causal promotions periodically so the graph stays current
                if changes_since_promotion >= _PROMOTION_INTERVAL:
                    await promote_high_fan_in_to_drives(db, project_id)
                    await promote_test_assertions(db, project_id)
                    await promote_mutation_drives(db, project_id)
                    changes_since_promotion = 0
                    log.debug("[daemon] causal graph promotions refreshed")
                    cycles = await detect_causal_cycles(db, project_id)
                    for cycle in cycles:
                        log.warning("[daemon] causal cycle detected: %s", cycle)

            except Exception as exc:
                log.warning("[daemon] failed to process event: %s", exc)

            queue.task_done()


def _start_http_server(queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
    try:
        server = HTTPServer(("127.0.0.1", DAEMON_PORT), _HookServer)
    except OSError as exc:
        log.warning("[daemon] hook server could not bind to port %d: %s — hook events disabled", DAEMON_PORT, exc)
        return
    server.queue = queue  # type: ignore[attr-defined]
    server.loop = loop    # type: ignore[attr-defined]
    server.serve_forever()


def run(project_root: Path) -> None:
    """Entry point — start the daemon for the given project root."""
    import os

    log_path = project_root / ".memory" / "daemon.log"
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
        )

    config = load_project_config(project_root)
    project_id = config["project_id"]

    pid_path = project_root / ".memory" / "daemon.pid"
    pid_path.write_text(str(os.getpid()))

    # Wait for SurrealDB to be reachable (it may still be starting up on login)
    from .cli import _surreal_reachable, _ensure_surreal_running
    for attempt in range(20):
        if _surreal_reachable():
            break
        log.info("[daemon] waiting for SurrealDB (attempt %d/20)...", attempt + 1)
        time.sleep(1)
    else:
        _ensure_surreal_running()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    queue: asyncio.Queue = asyncio.Queue()

    observer = Observer()
    observer.schedule(
        _ProjectFileHandler(queue, loop),
        str(project_root),
        recursive=True,
    )
    observer.start()

    http_thread = Thread(
        target=_start_http_server, args=(queue, loop), daemon=True
    )
    http_thread.start()

    log.info("[daemon] started — project_id=%s root=%s", project_id, project_root)

    async def _run() -> None:
        task = asyncio.ensure_future(
            _process_events(queue, project_id, project_root, provider_config=config)
        )
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        loop.run_until_complete(_run())
    finally:
        observer.stop()
        observer.join()
        loop.close()
        pid_path.unlink(missing_ok=True)
        log.info("[daemon] stopped.")
