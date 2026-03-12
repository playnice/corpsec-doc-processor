"""
CorpSec Document Processor — Main Entry Point

Pipeline:
  1. Watch inbox folder for new scanned PDFs
  2. OCR to extract text
  3. Ollama/Llama3 to extract metadata (company, doc type, date)
  4. Rename file: YYYYMMDD ShortName-Document Type.pdf
  5. Upload to Teamwork under matching project/entity

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
from ai_analyzer import analyze_document, analyze_document_vision
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
# Pipeline
# ---------------------------------------------------------------------------

teamwork = TeamworkUploader()


def process_pdf(pdf_path: Path) -> None:
    """Full processing pipeline for a single PDF."""
    logger.info("=" * 60)
    logger.info("Processing: %s", pdf_path.name)

    # Step 1: Try direct text extraction (works for digitally-created PDFs)
    logger.info("[1/4] Extracting text...")
    text = extract_text(pdf_path)

    # Step 2: AI analysis — choose path based on whether text was found
    if len(text) > 50:
        # Digital PDF: analyse the extracted text
        logger.info("[2/4] Analyzing text with %s...", config.OLLAMA_MODEL)
        metadata = analyze_document(text)
    else:
        # Scanned/image PDF: send page images directly to the vision model
        logger.info("[2/4] Scanned PDF detected — analyzing with vision model %s...", config.OLLAMA_VISION_MODEL)
        metadata = analyze_document_vision(pdf_path)
    logger.info(
        "Extracted — Company: %s | Type: %s | Date: %s | Confidence: %s",
        metadata.company_name,
        metadata.document_type,
        metadata.document_date,
        metadata.confidence,
    )

    if metadata.confidence == "low" and not metadata.company_name:
        logger.warning("Low confidence with no company name — moving to errors.")
        _move_to_errors(pdf_path)
        return

    # Step 3: Rename
    logger.info("[3/4] Renaming file...")
    renamed_path = rename_pdf(
        source=pdf_path,
        metadata=metadata,
        destination_folder=config.PROCESSED_FOLDER,
    )

    if not renamed_path:
        logger.error("Rename failed — moving original to errors.")
        _move_to_errors(pdf_path)
        return

    logger.info("File renamed to: %s", renamed_path.name)

    # Step 4: Upload to Teamwork
    logger.info("[4/4] Uploading to Teamwork...")
    category = _map_doc_type_to_category(metadata.document_type)
    uploaded = teamwork.upload_document(renamed_path, metadata, category_name=category)

    if uploaded:
        logger.info("Successfully uploaded to Teamwork.")
    else:
        logger.warning("Teamwork upload skipped or failed (file still saved locally).")

    logger.info("Done: %s", renamed_path.name)
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


def _map_doc_type_to_category(doc_type: str | None) -> str | None:
    """
    Map common corporate secretary document types to Teamwork categories.
    Customize this mapping for your organization.
    """
    if not doc_type:
        return None

    doc_lower = doc_type.lower()

    category_map = {
        "board resolution": "Resolutions",
        "directors resolution": "Resolutions",
        "shareholder resolution": "Resolutions",
        "authority to issue shares": "Share Capital",
        "share allotment": "Share Capital",
        "share transfer": "Share Capital",
        "return of allotment": "Share Capital",
        "certificate of incorporation": "Incorporation",
        "memorandum": "Constitution",
        "articles of association": "Constitution",
        "constitution": "Constitution",
        "annual return": "Annual Filing",
        "annual general meeting": "Meetings",
        "notice of agm": "Meetings",
        "minutes": "Meetings",
        "change of director": "Officers",
        "change of secretary": "Officers",
        "registered office": "Registered Office",
    }

    for keyword, category in category_map.items():
        if keyword in doc_lower:
            return category

    return "General"


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_watch_mode() -> None:
    """Continuously watch the inbox folder for new PDFs."""
    logger.info("Starting CorpSec Document Processor — Watch Mode")
    logger.info("Inbox folder: %s", config.WATCH_FOLDER)
    logger.info("Processed folder: %s", config.PROCESSED_FOLDER)
    logger.info("Ollama text model:   %s", config.OLLAMA_MODEL)
    logger.info("Ollama vision model: %s", config.OLLAMA_VISION_MODEL)

    # Ensure folders exist
    config.WATCH_FOLDER.mkdir(parents=True, exist_ok=True)
    config.PROCESSED_FOLDER.mkdir(parents=True, exist_ok=True)
    config.ERROR_FOLDER.mkdir(parents=True, exist_ok=True)

    # Process any PDFs already sitting in the folder
    existing = list(config.WATCH_FOLDER.glob("*.pdf"))
    if existing:
        logger.info("Found %d existing PDF(s) — processing...", len(existing))
        for pdf in existing:
            process_pdf(pdf)

    # Start watching for new files
    observer = start_watching(config.WATCH_FOLDER, process_pdf)

    # Graceful shutdown on Ctrl+C
    def shutdown(signum, frame):
        logger.info("Shutting down...")
        observer.stop()
        observer.join()
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


def run_single_file(file_path: str) -> None:
    """Process a single PDF file and exit."""
    pdf = Path(file_path)
    if not pdf.exists():
        logger.error("File not found: %s", pdf)
        sys.exit(1)
    if pdf.suffix.lower() != ".pdf":
        logger.error("Not a PDF file: %s", pdf)
        sys.exit(1)
    process_pdf(pdf)


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
