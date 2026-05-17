"""Background daemon — receives hook events and watches the file system."""

import asyncio
import json
import logging
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .db import DAEMON_PORT, get_db, ensure_schema, load_project_config
from .processor import process_file
from .sdk import store_event

log = logging.getLogger("[daemon]")

_WATCH_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
    ".md", ".yaml", ".yml", ".toml", ".sql", ".sh",
}

# Hook tools that mean a file was read or written — trigger full processing
_FILE_READ_TOOLS = {"Read", "NotebookEdit"}
_FILE_WRITE_TOOLS = {"Edit", "Write"}
_FILE_TOOLS = _FILE_READ_TOOLS | _FILE_WRITE_TOOLS


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
) -> None:
    """Consume events from the queue and persist them to SurrealDB."""
    async with get_db() as db:
        await ensure_schema(db)
        log.info("[daemon] ready — project_id=%s", project_id)

        while True:
            event = await queue.get()

            try:
                kind = event.get("kind")

                if kind == "file_changed":
                    # File saved on disk — run full dual-representation processing
                    path = Path(event["path"])
                    if path.is_file():
                        await process_file(path, project_root, project_id, db)
                        log.debug("[daemon] processed changed file: %s", path)

                else:
                    # Claude Code PostToolUse hook event
                    tool_name = event.get("tool_name", "")
                    tool_input = event.get("tool_input", {})

                    if tool_name in _FILE_TOOLS:
                        # Claude read or edited a file — run full processing
                        file_path = tool_input.get("file_path", "")
                        if file_path:
                            path = Path(file_path)
                            if path.is_file() and project_root in path.parents:
                                await process_file(path, project_root, project_id, db)
                                log.debug("[daemon] processed hook file: %s", path)

                    elif tool_name == "Bash":
                        # Log meaningful bash commands as episodic events
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

            except Exception as exc:
                log.warning("[daemon] failed to process event: %s", exc)

            queue.task_done()


def _start_http_server(queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
    server = HTTPServer(("127.0.0.1", DAEMON_PORT), _HookServer)
    server.queue = queue  # type: ignore[attr-defined]
    server.loop = loop    # type: ignore[attr-defined]
    server.serve_forever()


def run(project_root: Path) -> None:
    """Entry point — start the daemon for the given project root."""
    config = load_project_config(project_root)
    project_id = config["project_id"]

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

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)

    try:
        loop.run_until_complete(_process_events(queue, project_id, project_root))
    finally:
        observer.stop()
        observer.join()
        loop.close()
