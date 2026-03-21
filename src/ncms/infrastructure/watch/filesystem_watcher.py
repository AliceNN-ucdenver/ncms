"""FilesystemWatcher — watchdog integration with asyncio bridge and debounce.

Monitors directories for file changes using the watchdog library,
bridges events to asyncio via a queue, and debounces rapid changes
(common with editors that save multiple times).

Requires: pip install watchdog  (or pip install ncms[watch])
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ncms.application.watch_service import WatchService
from ncms.domain.watch import FileChangeEvent, FileChangeType, WatchStats

logger = logging.getLogger(__name__)

# Dynamic import — watchdog is optional
try:
    from watchdog.events import (
        FileSystemEventHandler,
    )
    from watchdog.observers import Observer

    _HAS_WATCHDOG = True
except ImportError:  # pragma: no cover
    _HAS_WATCHDOG = False
    Observer = None  # type: ignore[assignment,misc]
    FileSystemEventHandler = object  # type: ignore[assignment,misc]


class _AsyncEventHandler(FileSystemEventHandler if _HAS_WATCHDOG else object):  # type: ignore[misc]
    """Bridges watchdog thread events to an asyncio queue."""

    def __init__(
        self,
        queue: asyncio.Queue[tuple[str, str, float]],
        loop: asyncio.AbstractEventLoop,
        exclude_patterns: list[str] | None = None,
    ) -> None:
        self._queue = queue
        self._loop = loop
        self._exclude_patterns = exclude_patterns or [
            "*.pyc", "__pycache__", ".git", ".DS_Store", "*.swp", "*.swo",
            "*~", "*.tmp", ".#*",
        ]

    def _is_excluded(self, path: str) -> bool:
        from fnmatch import fnmatch

        name = Path(path).name
        for pattern in self._exclude_patterns:
            if fnmatch(name, pattern) or fnmatch(path, pattern):
                return True
        return False

    def on_created(self, event: Any) -> None:
        if not getattr(event, "is_directory", False) and not self._is_excluded(event.src_path):
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait,
                (event.src_path, "created", time.monotonic()),
            )

    def on_modified(self, event: Any) -> None:
        if not getattr(event, "is_directory", False) and not self._is_excluded(event.src_path):
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait,
                (event.src_path, "modified", time.monotonic()),
            )


class FilesystemWatcher:
    """Watches directories for file changes with debounce and asyncio integration.

    Usage:
        watcher = FilesystemWatcher(watch_service, debounce_seconds=2.0)
        await watcher.start([("/path/to/watch", True)])  # (path, recursive)
        # ... runs until stopped
        await watcher.stop()
    """

    def __init__(
        self,
        watch_service: WatchService,
        *,
        debounce_seconds: float = 2.0,
        exclude_patterns: list[str] | None = None,
    ) -> None:
        if not _HAS_WATCHDOG:
            msg = (
                "watchdog is required for filesystem watching. Install with:\n"
                "  pip install ncms[watch]\n"
                "  # or: pip install watchdog"
            )
            raise ImportError(msg)

        self._watch_service = watch_service
        self._debounce_seconds = debounce_seconds
        self._exclude_patterns = exclude_patterns
        self._observer: Any = None  # watchdog Observer
        self._queue: asyncio.Queue[tuple[str, str, float]] = asyncio.Queue()
        self._consumer_task: asyncio.Task[None] | None = None
        self._running = False
        self._watch_roots: list[Path] = []

    @property
    def running(self) -> bool:
        return self._running

    async def start(
        self,
        paths: list[tuple[str, bool]],
    ) -> None:
        """Start watching directories.

        Args:
            paths: List of (directory_path, recursive) tuples.
        """
        if self._running:
            logger.warning("Watcher already running")
            return

        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()

        handler = _AsyncEventHandler(
            self._queue, loop, self._exclude_patterns,
        )

        self._observer = Observer()
        for path_str, recursive in paths:
            path = Path(path_str)
            if not path.is_dir():
                logger.warning("Not a directory, skipping: %s", path)
                continue
            self._watch_roots.append(path)
            self._observer.schedule(handler, str(path), recursive=recursive)
            logger.info("Watching: %s (recursive=%s)", path, recursive)

        self._observer.start()
        self._running = True
        self._consumer_task = asyncio.create_task(self._process_events())

        # Initial scan of all watched directories
        for path_str, recursive in paths:
            path = Path(path_str)
            if path.is_dir():
                await self._watch_service.scan_directory(path, recursive=recursive)

        logger.info("Filesystem watcher started (%d directories)", len(self._watch_roots))

    async def stop(self) -> None:
        """Stop watching and clean up."""
        self._running = False

        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

        if self._consumer_task:
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None

        # Persist hashes on shutdown
        await self._watch_service.save_hashes()
        logger.info("Filesystem watcher stopped")

    async def _process_events(self) -> None:
        """Consumer coroutine: drains event queue with debounce."""
        pending: dict[str, tuple[str, float]] = {}  # path -> (change_type, timestamp)

        while self._running:
            try:
                # Drain all available events
                try:
                    while True:
                        path, change_type, ts = self._queue.get_nowait()
                        pending[path] = (change_type, ts)
                except asyncio.QueueEmpty:
                    pass

                if not pending:
                    # Wait for next event with timeout
                    try:
                        path, change_type, ts = await asyncio.wait_for(
                            self._queue.get(), timeout=1.0,
                        )
                        pending[path] = (change_type, ts)
                    except TimeoutError:
                        continue

                # Wait for debounce window
                await asyncio.sleep(self._debounce_seconds)

                # Drain any events that arrived during debounce
                try:
                    while True:
                        path, change_type, ts = self._queue.get_nowait()
                        pending[path] = (change_type, ts)
                except asyncio.QueueEmpty:
                    pass

                # Process all debounced events
                now = time.monotonic()
                ready = {
                    p: (ct, t) for p, (ct, t) in pending.items()
                    if now - t >= self._debounce_seconds
                }

                for path, (change_type, _ts) in ready.items():
                    del pending[path]
                    event = FileChangeEvent(
                        path=path,
                        change_type=(
                            FileChangeType.CREATED if change_type == "created"
                            else FileChangeType.MODIFIED
                        ),
                        timestamp=datetime.now(UTC),
                    )
                    # Find the matching watch root
                    watch_root = None
                    file_path = Path(path)
                    for root in self._watch_roots:
                        try:
                            file_path.relative_to(root)
                            watch_root = root
                            break
                        except ValueError:
                            continue

                    await self._watch_service.handle_file_event(
                        event, watch_root=watch_root,
                    )

                # Persist hashes periodically
                if ready:
                    await self._watch_service.save_hashes()

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error processing watch events")
                await asyncio.sleep(1.0)

    def get_stats(self) -> WatchStats:
        """Get current watch statistics."""
        return self._watch_service.stats
