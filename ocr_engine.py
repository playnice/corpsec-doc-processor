"""
OCR Engine module.
Extracts text from scanned PDF documents using OCRmyPDF and PyMuPDF.
"""

import logging
import re
import subprocess
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

import config

logger = logging.getLogger(__name__)

# Tesseract user-words file to bias OCR toward known company name tokens
_USER_WORDS_FILE = Path(__file__).parent / "tesseract_words.txt"


def extract_text(pdf_path: Path) -> str:
    """
    Extract text from a PDF. First tries direct text extraction (for
    digitally-created PDFs). If that yields little text, falls back to OCR.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        Extracted text content.
    """
    # Step 1: Try direct text extraction with PyMuPDF
    text = _extract_text_direct(pdf_path)

    if len(text.strip()) > 50:
        logger.info("Direct text extraction succeeded (%d chars)", len(text))
        return text.strip()

    # Step 2: Fall back to OCR
    logger.info("Direct extraction yielded little text, running OCR...")
    text = _extract_text_ocr(pdf_path)
    logger.info("OCR extraction complete (%d chars)", len(text))
    return text.strip()


def _extract_text_direct(pdf_path: Path) -> str:
    """Extract text directly from PDF using PyMuPDF."""
    text_parts = []
    try:
        doc = fitz.open(str(pdf_path))
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()
    except Exception:
        logger.exception("Direct text extraction failed for %s", pdf_path.name)
    return "\n".join(text_parts)


def _extract_text_ocr(pdf_path: Path) -> str:
    """
    Run OCRmyPDF to produce a searchable PDF, then extract text from it.
    OCRmyPDF wraps Tesseract and handles image pre-processing automatically.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        cmd = [
            "ocrmypdf",
            "--force-ocr",          # OCR even if text layer exists
            "--deskew",             # straighten tilted scans
            "--clean",              # clean page images before OCR
            "--oversample", "300",  # upscale low-res scans to 300 DPI
            "--output-type", "pdf",
            "--tesseract-timeout", "120",
        ]

        # Provide a custom word list so Tesseract prefers "ZOO" over "200" etc.
        if _USER_WORDS_FILE.exists():
            cmd += ["--user-words", str(_USER_WORDS_FILE)]

        cmd += [str(pdf_path), str(tmp_path)]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode not in (0, 6):
            # Return code 6 = "file already has text" (still okay)
            logger.warning("OCRmyPDF returned code %d: %s", result.returncode, result.stderr)

        # Extract text from the OCR'd PDF
        text = _extract_text_direct(tmp_path)
        return text

    except subprocess.TimeoutExpired:
        logger.error("OCR timed out for %s", pdf_path.name)
        return ""
    except FileNotFoundError:
        logger.error(
            "ocrmypdf not found. Install it: pip install ocrmypdf "
            "and ensure Tesseract is installed."
        )
        return ""
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
