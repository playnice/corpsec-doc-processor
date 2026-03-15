"""
Folder watcher module.
Monitors the inbox folder for new PDF files and triggers the processing pipeline.
Uses a thread-safe queue so that file processing always happens on the main thread
(required by Playwright which uses greenlets and cannot switch threads).
"""

import logging
import queue
import time
from pathlib import Path
from typing import Callable

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent

logger = logging.getLogger(__name__)


class PDFHandler(FileSystemEventHandler):
    """Handles new PDF files appearing in the watched folder."""

    def __init__(self, file_queue: queue.Queue, stable_seconds: float = 3.0):
        super().__init__()
        self._queue = file_queue
        self.stable_seconds = stable_seconds

    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != ".pdf":
            return

        logger.info("New PDF detected: %s", path.name)
        self._wait_until_stable(path)
        self._queue.put(path)

    def _wait_until_stable(self, path: Path) -> None:
        """Wait until the file size stops changing."""
        previous_size = -1
        while True:
            try:
                current_size = path.stat().st_size
            except OSError:
                time.sleep(0.5)
                continue
            if current_size == previous_size and current_size > 0:
                break
            previous_size = current_size
            time.sleep(self.stable_seconds)


def start_watching(folder: Path) -> tuple[Observer, queue.Queue]:
    """
    Start watching a folder for new PDF files.

    Returns:
        (observer, file_queue) — poll file_queue on the main thread.
    """
    folder.mkdir(parents=True, exist_ok=True)
    file_queue: queue.Queue[Path] = queue.Queue()
    handler = PDFHandler(file_queue)
    observer = Observer()
    observer.schedule(handler, str(folder), recursive=False)
    observer.start()
    logger.info("Watching folder: %s", folder)
    return observer, file_queue
