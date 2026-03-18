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
import queue
import re
import shutil
import sys
import time
from pathlib import Path

import config
from ocr_engine import extract_text
from ai_analyzer import analyze_document
from renamer import rename_pdf
from pdf_splitter import split_pdf
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
MODE_SPLIT_PDF = 4

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
    print("  4. Split Combined PDF")
    print("=" * 50)

    while True:
        choice = input("\n  Enter choice (1/2/3/4): ").strip()
        if choice in ("1", "2", "3", "4"):
            return int(choice)
        print("  Invalid choice. Please enter 1, 2, 3, or 4.")

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

# Pattern: "YYYYMMDD SHORTNAME - DocType" or "YYYYMMDD SHORTNAME-DocType"
_FILENAME_PATTERN = re.compile(
    r"^(\d{8})\s+([A-Z0-9]+(?:\s[A-Z0-9]+)*)\s*[-–—]\s*(.+)$"
)


def _parse_metadata_from_filename(stem: str):
    """Parse all metadata from a pre-renamed filename.

    Format: 'YYYYMMDD SHORTNAME - DocType (extra info)'
    Returns (company_full_name, doc_type, date_str) or (None, None, None).
    """
    from ai_analyzer import DocumentMetadata

    m = _FILENAME_PATTERN.match(stem)
    if not m:
        logger.warning("Could not parse filename: %s", stem)
        return None

    date_raw = m.group(1)        # e.g. "20221103"
    short_name = m.group(2)      # e.g. "BXI"
    doc_type = m.group(3).strip() # e.g. "Disclosure of Interest (Vincent)"

    # Date: YYYYMMDD → YYYY-MM-DD
    date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"

    # Company: look up full name from Entity List CSV
    company = config.get_company_full_name(short_name)
    if company:
        logger.info("Filename → Company: '%s' → '%s'", short_name, company)
    else:
        logger.error("Filename → Abbreviation '%s' not found in Entity List CSV", short_name)

    logger.info("Filename → Type: '%s' | Date: %s", doc_type, date_str)

    return DocumentMetadata(
        company_name=company,
        document_type=doc_type,
        document_content=doc_type,
        document_date=date_str,
        confidence="high",
    )


def process_pdf(pdf_path: Path) -> None:
    """Full processing pipeline for a single PDF."""
    global processing_mode, teamwork

    logger.info("=" * 60)
    logger.info("Processing: %s", pdf_path.name)

    # --- Mode 4: Split Combined PDF ---
    if processing_mode == MODE_SPLIT_PDF:
        logger.info("[Split] Splitting combined PDF: %s", pdf_path.name)
        created = split_pdf(pdf_path, output_folder=config.RENAMED_FOLDER)
        if created:
            logger.info("Split into %d file(s) → %s", len(created), config.RENAMED_FOLDER)
            # Move original to Uploaded folder to indicate it's been processed
            config.UPLOADED_FOLDER.mkdir(parents=True, exist_ok=True)
            dest = config.UPLOADED_FOLDER / pdf_path.name
            _move_with_retry(pdf_path, dest)
        else:
            logger.error("Split failed — moving to errors.")
            _move_to_errors(pdf_path)
        logger.info("=" * 60)
        return

    if processing_mode == MODE_UPLOAD_ONLY:
        # File is already renamed — parse ALL metadata from filename
        # Format: "YYYYMMDD SHORTNAME - DocType.pdf"
        filename_meta = _parse_metadata_from_filename(pdf_path.stem)

        if not filename_meta:
            logger.error("Cannot parse filename — skipping: %s", pdf_path.name)
            _move_to_errors(pdf_path)
            return

        metadata = filename_meta

        # Only run OCR + AI if company not resolved from config
        if not metadata.company_name:
            logger.info("[1] Extracting text for company detection...")
            text = extract_text(pdf_path)
            if text:
                logger.info("Extracted %d characters of text.", len(text))
                logger.info("[2] Analyzing with %s...", config.OLLAMA_MODEL)
                ai_meta = analyze_document(text)
                logger.info("AI company: %s", ai_meta.company_name)
                if ai_meta.company_name:
                    metadata.company_name = ai_meta.company_name

        if not metadata.company_name:
            logger.error("Could not determine company — skipping: %s", pdf_path.name)
            _move_to_errors(pdf_path)
            return

        logger.info(
            "Final metadata — Company: %s | Type: %s | Description: %s | Date: %s",
            metadata.company_name, metadata.document_type,
            metadata.document_content, metadata.document_date,
        )
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
        _move_with_retry(file_path, dest)
    else:
        logger.warning("Teamwork upload failed (file remains at: %s).", file_path)

    logger.info("Done: %s", file_path.name)
    logger.info("=" * 60)


def _move_with_retry(src: Path, dest: Path, max_retries: int = 3, wait: int = 5) -> bool:
    """Move a file, retrying if it's locked (e.g. open in a PDF reader)."""
    for attempt in range(max_retries):
        try:
            shutil.move(str(src), str(dest))
            logger.info("Moved to: %s", dest)
            return True
        except PermissionError:
            remaining = max_retries - attempt - 1
            if remaining > 0:
                logger.warning(
                    "File is locked (open in another program?): %s — "
                    "retrying in %ds (%d retries left). Close the file to continue.",
                    src.name, wait, remaining,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "Could not move file after %d attempts (still locked): %s",
                    max_retries, src.name,
                )
        except OSError:
            logger.exception("Could not move file: %s", src.name)
            return False
    return False


def _move_to_errors(pdf_path: Path) -> None:
    """Move a problem file to the errors folder for manual review."""
    config.ERROR_FOLDER.mkdir(parents=True, exist_ok=True)
    dest = config.ERROR_FOLDER / pdf_path.name
    _move_with_retry(pdf_path, dest)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

MODE_LABELS = {
    MODE_RENAME_ONLY: "Rename Only",
    MODE_RENAME_AND_UPLOAD: "Rename & Upload to Teamwork",
    MODE_UPLOAD_ONLY: "Upload to Teamwork Only",
    MODE_SPLIT_PDF: "Split Combined PDF",
}


def run_watch_mode() -> None:
    """Continuously watch the inbox folder for new PDFs."""
    global processing_mode, teamwork

    processing_mode = prompt_mode()

    # Upload-only watches Renamed folder; Split watches Split folder; others watch Inbox
    if processing_mode == MODE_UPLOAD_ONLY:
        watch_folder = config.RENAMED_FOLDER
    elif processing_mode == MODE_SPLIT_PDF:
        watch_folder = config.SPLIT_FOLDER
    else:
        watch_folder = config.WATCH_FOLDER

    logger.info("Starting CorpSec Document Processor — Watch Mode")
    logger.info("Mode: %s", MODE_LABELS[processing_mode])
    logger.info("Watch folder: %s", watch_folder)
    logger.info("Renamed folder: %s", config.RENAMED_FOLDER)
    logger.info("Uploaded folder: %s", config.UPLOADED_FOLDER)
    logger.info("Ollama model: %s", config.OLLAMA_MODEL)

    # Ensure folders exist
    config.WATCH_FOLDER.mkdir(parents=True, exist_ok=True)
    config.SPLIT_FOLDER.mkdir(parents=True, exist_ok=True)
    config.RENAMED_FOLDER.mkdir(parents=True, exist_ok=True)
    config.UPLOADED_FOLDER.mkdir(parents=True, exist_ok=True)
    config.ERROR_FOLDER.mkdir(parents=True, exist_ok=True)

    # Init Teamwork uploader if needed
    if processing_mode in (MODE_RENAME_AND_UPLOAD, MODE_UPLOAD_ONLY):
        teamwork = TeamworkUploader()

    # Process any PDFs already sitting in the folder
    existing = set(watch_folder.glob("*.pdf"))
    if existing:
        logger.info("Found %d existing PDF(s) — processing...", len(existing))
        for pdf in sorted(existing):
            process_pdf(pdf)

    # Start watching for new files (queue-based, so processing stays on main thread)
    observer, file_queue = start_watching(watch_folder)

    # Re-scan for files added during initial batch processing
    missed = sorted(f for f in watch_folder.glob("*.pdf") if f not in existing)
    if missed:
        logger.info("Found %d file(s) added during batch — queuing...", len(missed))
        for pdf in missed:
            file_queue.put(pdf)

    logger.info("Watching for new PDFs... (Ctrl+C to stop)")
    try:
        while True:
            try:
                pdf_path = file_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                process_pdf(pdf_path)
            except Exception:
                logger.exception("Error processing %s", pdf_path.name)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Shutting down...")
        observer.stop()
        observer.join()
        try:
            if teamwork:
                teamwork.close()
        except Exception:
            pass


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
