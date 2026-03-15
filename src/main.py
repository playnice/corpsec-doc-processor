"""
CorpSec Document Processor — Main Entry Point

Pipeline:
  1. Watch inbox folder for new scanned PDFs
  2. OCR to extract text
  3. Ollama/Llama3.2 to extract metadata (company, doc type, date)
  4. Based on selected mode:
     - Rename Only:            rename → move to Renamed folder
     - Rename & Upload:        rename → upload to Teamwork → move to Uploaded folder
     - Upload Only:            upload existing file to Teamwork → move to Uploaded folder

Usage:
  python main.py              # Watch mode (continuous)
  python main.py --once FILE  # Process a single file and exit
"""

import argparse
import logging
import shutil
import signal
import sys
import time
from pathlib import Path

import config
from ocr_engine import extract_text
from ai_analyzer import analyze_document
from renamer import rename_pdf
from teamwork_uploader import TeamworkUploader
from watcher import start_watching

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("corpsec")

# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------

MODE_RENAME_ONLY = 1
MODE_RENAME_AND_UPLOAD = 2
MODE_UPLOAD_ONLY = 3

processing_mode: int = MODE_RENAME_ONLY
teamwork: TeamworkUploader | None = None


def prompt_mode() -> int:
    """Ask the user which processing mode to use."""
    print("\n" + "=" * 50)
    print("  CorpSec Document Processor")
    print("=" * 50)
    print("  Select processing mode:\n")
    print("  1. Rename Only")
    print("  2. Rename & Upload to Teamwork")
    print("  3. Upload to Teamwork Only")
    print("=" * 50)

    while True:
        choice = input("\n  Enter choice (1/2/3): ").strip()
        if choice in ("1", "2", "3"):
            return int(choice)
        print("  Invalid choice. Please enter 1, 2, or 3.")

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def process_pdf(pdf_path: Path) -> None:
    """Full processing pipeline for a single PDF."""
    global processing_mode, teamwork

    logger.info("=" * 60)
    logger.info("Processing: %s", pdf_path.name)

    if processing_mode == MODE_UPLOAD_ONLY:
        # Run OCR/AI for metadata (needed for upload fields) but skip rename
        logger.info("[1] Extracting text...")
        text = extract_text(pdf_path)
        metadata = None
        if text:
            logger.info("Extracted %d characters of text.", len(text))
            logger.info("[2] Analyzing with %s...", config.OLLAMA_MODEL)
            metadata = analyze_document(text)
            logger.info(
                "Extracted — Company: %s | Type: %s | Content: %s | Date: %s",
                metadata.company_name, metadata.document_type,
                metadata.document_content, metadata.document_date,
            )
        else:
            logger.warning("No text extracted — uploading without metadata.")
        _upload_and_move(pdf_path, metadata=metadata)
        return

    # Step 1: OCR / text extraction
    logger.info("[1] Extracting text...")
    text = extract_text(pdf_path)
    if not text:
        logger.error("No text extracted from %s — moving to errors folder.", pdf_path.name)
        _move_to_errors(pdf_path)
        return

    logger.info("Extracted %d characters of text.", len(text))
    logger.debug("First 500 chars: %s", text[:500])

    # Step 2: AI analysis
    logger.info("[2] Analyzing with %s...", config.OLLAMA_MODEL)
    metadata = analyze_document(text)
    logger.info(
        "Extracted — Company: %s | Type: %s | Content: %s | Date: %s | Confidence: %s",
        metadata.company_name,
        metadata.document_type,
        metadata.document_content,
        metadata.document_date,
        metadata.confidence,
    )
    if metadata.position_held or metadata.has_cessation or metadata.is_multi_entry:
        logger.info(
            "  Officer-change fields — Position: %s | Cessation: %s | Multi-entry: %s | All-cessation: %s",
            metadata.position_held, metadata.has_cessation, metadata.is_multi_entry, metadata.all_cessation,
        )

    if metadata.confidence == "low" and not metadata.company_name:
        logger.warning("Low confidence with no company name — moving to errors.")
        _move_to_errors(pdf_path)
        return

    # Step 3: Rename
    logger.info("[3] Renaming file...")
    renamed_path = rename_pdf(
        source=pdf_path,
        metadata=metadata,
        destination_folder=config.RENAMED_FOLDER,
    )

    if not renamed_path:
        logger.error("Rename failed — moving original to errors.")
        _move_to_errors(pdf_path)
        return

    logger.info("File renamed to: %s", renamed_path.name)

    if processing_mode == MODE_RENAME_ONLY:
        logger.info("Done (rename only): %s", renamed_path.name)
        logger.info("=" * 60)
        return

    # Step 4: Upload to Teamwork (mode 2 only)
    _upload_and_move(renamed_path, metadata)


def _upload_and_move(file_path: Path, metadata=None) -> None:
    """Upload a file to Teamwork and move it to the Uploaded folder."""
    global teamwork

    logger.info("[Upload] Uploading to Teamwork: %s", file_path.name)

    if not teamwork:
        teamwork = TeamworkUploader()

    uploaded = teamwork.upload_document(file_path, metadata)

    if uploaded:
        logger.info("Successfully uploaded to Teamwork.")
        # Move to Uploaded folder
        config.UPLOADED_FOLDER.mkdir(parents=True, exist_ok=True)
        dest = config.UPLOADED_FOLDER / file_path.name
        try:
            shutil.move(str(file_path), str(dest))
            logger.info("Moved to uploaded: %s", dest)
        except OSError:
            logger.exception("Could not move file to uploaded folder")
    else:
        logger.warning("Teamwork upload failed (file remains at: %s).", file_path)

    logger.info("Done: %s", file_path.name)
    logger.info("=" * 60)


def _move_to_errors(pdf_path: Path) -> None:
    """Move a problem file to the errors folder for manual review."""
    config.ERROR_FOLDER.mkdir(parents=True, exist_ok=True)
    dest = config.ERROR_FOLDER / pdf_path.name
    try:
        shutil.move(str(pdf_path), str(dest))
        logger.info("Moved to errors: %s", dest)
    except OSError:
        logger.exception("Could not move file to errors folder")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

MODE_LABELS = {
    MODE_RENAME_ONLY: "Rename Only",
    MODE_RENAME_AND_UPLOAD: "Rename & Upload to Teamwork",
    MODE_UPLOAD_ONLY: "Upload to Teamwork Only",
}


def run_watch_mode() -> None:
    """Continuously watch the inbox folder for new PDFs."""
    global processing_mode, teamwork

    processing_mode = prompt_mode()

    # Upload-only watches the Renamed folder; other modes watch Inbox
    watch_folder = config.RENAMED_FOLDER if processing_mode == MODE_UPLOAD_ONLY else config.WATCH_FOLDER

    logger.info("Starting CorpSec Document Processor — Watch Mode")
    logger.info("Mode: %s", MODE_LABELS[processing_mode])
    logger.info("Watch folder: %s", watch_folder)
    logger.info("Renamed folder: %s", config.RENAMED_FOLDER)
    logger.info("Uploaded folder: %s", config.UPLOADED_FOLDER)
    logger.info("Ollama model: %s", config.OLLAMA_MODEL)

    # Ensure folders exist
    config.WATCH_FOLDER.mkdir(parents=True, exist_ok=True)
    config.RENAMED_FOLDER.mkdir(parents=True, exist_ok=True)
    config.UPLOADED_FOLDER.mkdir(parents=True, exist_ok=True)
    config.ERROR_FOLDER.mkdir(parents=True, exist_ok=True)

    # Init Teamwork uploader if needed
    if processing_mode in (MODE_RENAME_AND_UPLOAD, MODE_UPLOAD_ONLY):
        teamwork = TeamworkUploader()

    # Process any PDFs already sitting in the folder
    existing = list(watch_folder.glob("*.pdf"))
    if existing:
        logger.info("Found %d existing PDF(s) — processing...", len(existing))
        for pdf in existing:
            process_pdf(pdf)

    # Start watching for new files
    observer = start_watching(watch_folder, process_pdf)

    # Graceful shutdown on Ctrl+C
    def shutdown(signum, frame):
        logger.info("Shutting down...")
        observer.stop()
        observer.join()
        if teamwork:
            teamwork.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Watching for new PDFs... (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    if teamwork:
        teamwork.close()


def run_single_file(file_path: str) -> None:
    """Process a single PDF file and exit."""
    global processing_mode, teamwork

    processing_mode = prompt_mode()

    if processing_mode in (MODE_RENAME_AND_UPLOAD, MODE_UPLOAD_ONLY):
        teamwork = TeamworkUploader()

    pdf = Path(file_path)
    if not pdf.exists():
        logger.error("File not found: %s", pdf)
        sys.exit(1)
    if pdf.suffix.lower() != ".pdf":
        logger.error("Not a PDF file: %s", pdf)
        sys.exit(1)

    process_pdf(pdf)

    if teamwork:
        teamwork.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CorpSec Document Processor — OCR + AI rename + Teamwork upload"
    )
    parser.add_argument(
        "--once",
        metavar="FILE",
        help="Process a single PDF file and exit (instead of watch mode).",
    )
    args = parser.parse_args()

    if args.once:
        run_single_file(args.once)
    else:
        run_watch_mode()


if __name__ == "__main__":
    main()
