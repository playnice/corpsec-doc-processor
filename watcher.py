"""
Folder watcher module.
Monitors the inbox folder for new PDF files and triggers the processing pipeline.
"""

import logging
import time
from pathlib import Path
from typing import Callable

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent

logger = logging.getLogger(__name__)


class PDFHandler(FileSystemEventHandler):
    """Handles new PDF files appearing in the watched folder."""

    def __init__(self, callback: Callable[[Path], None], stable_seconds: float = 3.0):
        """
        Args:
            callback: Function to call with the Path of each new PDF.
            stable_seconds: Wait this long after creation to ensure the file
                            is fully written (drag-and-drop may take time).
        """
        super().__init__()
        self.callback = callback
        self.stable_seconds = stable_seconds

    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != ".pdf":
            return

        logger.info("New PDF detected: %s", path.name)

        # Wait for the file to be fully written (stable file size)
        self._wait_until_stable(path)

        try:
            self.callback(path)
        except Exception:
            logger.exception("Error processing %s", path.name)

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


def start_watching(folder: Path, callback: Callable[[Path], None]) -> Observer:
    """
    Start watching a folder for new PDF files.

    Args:
        folder: The directory to watch.
        callback: Called with Path of each new PDF.

    Returns:
        The Observer instance (call .stop() to shut down).
    """
    folder.mkdir(parents=True, exist_ok=True)
    handler = PDFHandler(callback)
    observer = Observer()
    observer.schedule(handler, str(folder), recursive=False)
    observer.start()
    logger.info("Watching folder: %s", folder)
    return observer
