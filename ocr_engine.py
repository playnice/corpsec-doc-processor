"""
OCR Engine module.
Extracts text from digitally-created PDFs using PyMuPDF, and renders
PDF pages as images for vision-model analysis of scanned documents.
"""

import io
import logging
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

logger = logging.getLogger(__name__)

# Maximum pixel height for a merged image sent to the vision model.
# Pages are scaled down proportionally if this limit is exceeded.
_MAX_MERGED_HEIGHT = 8000


def extract_text(pdf_path: Path) -> str:
    """
    Extract text directly from a digitally-created PDF using PyMuPDF.
    Returns an empty string (or very short text) for scanned/image-only PDFs —
    callers should check length and fall back to vision analysis in that case.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        Extracted text content (may be empty for scanned PDFs).
    """
    text = extract_text_direct(pdf_path)
    if len(text.strip()) > 50:
        logger.info("Direct text extraction succeeded (%d chars)", len(text))
    else:
        logger.info("Direct extraction yielded little text — PDF is likely scanned.")
    return text.strip()


def extract_text_direct(pdf_path: Path) -> str:
    """Extract embedded text directly from a PDF using PyMuPDF."""
    text_parts = []
    try:
        doc = fitz.open(str(pdf_path))
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()
    except Exception:
        logger.exception("Direct text extraction failed for %s", pdf_path.name)
    return "\n".join(text_parts)


def merge_pages_to_image(pdf_path: Path, dpi: int = 150, max_pages: int = 6) -> bytes | None:
    """
    Render PDF pages and stitch them vertically into a single PNG image.
    Gives the vision model the full document context in one request.

    Pages are rendered at `dpi` resolution and composited top-to-bottom on a
    white canvas.  If the merged height exceeds _MAX_MERGED_HEIGHT the entire
    canvas is scaled down proportionally so the model receives a reasonably
    sized image.

    Args:
        pdf_path:  Path to the PDF file.
        dpi:       Render resolution per page (150 is a good default).
        max_pages: Maximum number of pages to include (caps very long documents).

    Returns:
        PNG image bytes of the merged page strip, or None on failure.
    """
    pil_pages: list[Image.Image] = []
    try:
        doc = fitz.open(str(pdf_path))
        pages = list(doc)[:max_pages]
        for page in pages:
            pix = page.get_pixmap(dpi=dpi)
            mode = "RGBA" if pix.alpha else "RGB"
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            if img.mode != "RGB":
                img = img.convert("RGB")
            pil_pages.append(img)
        doc.close()
    except Exception:
        logger.exception("Failed to render pages from %s", pdf_path.name)
        return None

    if not pil_pages:
        return None

    canvas_w = max(img.width for img in pil_pages)
    canvas_h = sum(img.height for img in pil_pages)

    # Scale down if the merged strip is too tall
    scale = 1.0
    if canvas_h > _MAX_MERGED_HEIGHT:
        scale = _MAX_MERGED_HEIGHT / canvas_h
        canvas_w = int(canvas_w * scale)
        canvas_h = _MAX_MERGED_HEIGHT
        pil_pages = [
            img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
            for img in pil_pages
        ]

    canvas = Image.new("RGB", (canvas_w, canvas_h), color=(255, 255, 255))
    y = 0
    for img in pil_pages:
        x = (canvas_w - img.width) // 2  # centre-align narrower pages
        canvas.paste(img, (x, y))
        y += img.height

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    merged_bytes = buf.getvalue()

    logger.info(
        "Merged %d page(s) from %s into single image (%dx%dpx, %.1f KB)",
        len(pil_pages), pdf_path.name, canvas_w, canvas_h, len(merged_bytes) / 1024,
    )
    return merged_bytes

