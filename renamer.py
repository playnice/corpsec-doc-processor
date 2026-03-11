"""
Renamer module.
Generates standardized filenames from document metadata and renames PDFs.
Format: YYYYMMDD CompanyShortName-Document Type.pdf
Example: 20220519 ZCFII-Authority to Issue Shares.pdf
"""

import logging
import re
import shutil
from pathlib import Path

import config
from ai_analyzer import DocumentMetadata

logger = logging.getLogger(__name__)

# Characters not allowed in Windows filenames
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def generate_filename(metadata: DocumentMetadata) -> str | None:
    """
    Generate a standardized filename from document metadata.

    Format: YYYYMMDD ShortName-Document Type.pdf
    Example: 20220519 ZCFII-Authority to Issue Shares.pdf

    Returns None if critical fields are missing.
    """
    if not metadata.document_date or not metadata.company_name:
        logger.warning(
            "Cannot generate filename: date=%s, company=%s",
            metadata.document_date,
            metadata.company_name,
        )
        return None

    # Format date: YYYY-MM-DD -> YYYYMMDD
    date_str = metadata.document_date.replace("-", "")
    if len(date_str) != 8 or not date_str.isdigit():
        logger.warning("Invalid date format: %s", metadata.document_date)
        return None

    # Get company short name
    short_name = config.get_company_short_name(metadata.company_name)

    # Document type (default if not detected)
    doc_type = metadata.document_type or "Document"
    # Clean up document type - Title Case, strip excess whitespace
    doc_type = " ".join(doc_type.split())

    # Assemble filename
    filename = f"{date_str} {short_name}-{doc_type}.pdf"

    # Sanitize: remove invalid characters
    filename = INVALID_FILENAME_CHARS.sub("", filename)

    # Trim length (Windows MAX_PATH consideration)
    if len(filename) > 200:
        base = filename[:-4]  # without .pdf
        filename = base[:196] + ".pdf"

    return filename


def rename_pdf(
    source: Path,
    metadata: DocumentMetadata,
    destination_folder: Path | None = None,
) -> Path | None:
    """
    Rename a PDF file based on extracted metadata.

    Args:
        source: Path to the original PDF.
        metadata: Extracted document metadata.
        destination_folder: If provided, move the renamed file here.
                           Otherwise, rename in place.

    Returns:
        Path to the renamed file, or None if renaming failed.
    """
    new_name = generate_filename(metadata)
    if not new_name:
        logger.error("Could not generate filename for %s", source.name)
        return None

    if destination_folder:
        destination_folder.mkdir(parents=True, exist_ok=True)
        new_path = destination_folder / new_name
    else:
        new_path = source.parent / new_name

    # Handle filename collisions by appending a counter
    if new_path.exists():
        stem = new_path.stem
        suffix = new_path.suffix
        counter = 1
        while new_path.exists():
            new_path = new_path.parent / f"{stem} ({counter}){suffix}"
            counter += 1

    try:
        shutil.move(str(source), str(new_path))
        logger.info("Renamed: %s -> %s", source.name, new_path.name)
        return new_path
    except OSError:
        logger.exception("Failed to rename %s", source.name)
        return None
