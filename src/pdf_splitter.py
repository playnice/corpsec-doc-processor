"""
PDF Splitter module.
Splits combined/multi-document scanned PDFs into individual documents.

Uses per-page OCR + Ollama AI to identify document boundaries,
then splits using PyMuPDF and names each output file using Entity List CSV.
"""

import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
import pytesseract
from PIL import Image

import ollama as ollama_client
import config

logger = logging.getLogger(__name__)

pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DocumentSegment:
    """A single document identified within a combined PDF."""
    page_start: int          # 1-based inclusive
    page_end: int            # 1-based inclusive
    company_name: str | None
    document_type: str | None
    document_date: str | None  # YYYY-MM-DD

    @property
    def abbreviation(self) -> str | None:
        """Look up company abbreviation from Entity List CSV."""
        if not self.company_name:
            return None
        return config.get_company_short_name(self.company_name)


# ---------------------------------------------------------------------------
# Per-page OCR
# ---------------------------------------------------------------------------

def _ocr_pages(pdf_path: Path, dpi: int = 200) -> list[str]:
    """Extract text from each page individually via Tesseract OCR.

    Returns a list of strings, one per page (0-indexed).
    """
    doc = fitz.open(str(pdf_path))
    page_texts: list[str] = []

    for page_num in range(doc.page_count):
        page = doc[page_num]

        # Try direct text first
        text = page.get_text().strip()
        if len(text) > 50:
            page_texts.append(text)
            continue

        # Fall back to OCR
        pix = page.get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        text = pytesseract.image_to_string(img)
        page_texts.append(text.strip())

    doc.close()
    return page_texts


# ---------------------------------------------------------------------------
# AI document-boundary detection
# ---------------------------------------------------------------------------

_SPLIT_PROMPT = """You are analyzing a {page_count}-page scanned PDF containing multiple corporate documents combined together. Below is OCR text from each page.

Your task: identify where each SEPARATE document begins and ends.

RULES:
- A new document starts ONLY when a page has a NEW "Directors' Resolution In Writing" (DRIW) with a DIFFERENT subject/topic than the previous one.
- Supporting pages after a resolution (application forms, bank transfers, subscription agreements, signature pages, notices, letters of authority) are ALL part of that same resolution — NOT separate documents.
- Two consecutive pages about the same topic are the SAME document, even if both have company letterheads.
- The entire PDF has exactly {page_count} pages. Do NOT reference pages beyond {page_count}.

COMMON DOCUMENT TYPES (use exactly the subject from the resolution heading):
- "DRIW-Appointment of Alternate Director"
- "DRIW-Appointment of Auditors"
- "DRIW-Allotment of Shares" (includes application forms, bank transfers, subscription agreements)
- "DRIW-Declaration of Interests" (includes banking resolutions, specimen signatures)
- "DRIW-Opening of Bank Account" (specify which bank if mentioned)
- "DRIW-Convening an EGM"
- "DRIW-Incorporation" (first board resolutions)
- "DRIW-AGM FYyyyy-mm" (Annual General Meeting — ONLY if AGM is explicitly mentioned)
- Or any other subject directly from the resolution heading

{page_texts}

For each document, provide:
- page_start / page_end (1-based, inclusive)
- company_name: full legal name from letterhead
- document_type: use format "DRIW-[Subject from resolution heading]"
- document_date: the RESOLUTION date (look for "Date: ..." or "Dated ..." near signatures), NOT financial year end. Format YYYY-MM-DD. If unclear, use null.

Return ONLY a JSON array:
[{{"page_start": 1, "page_end": 3, "company_name": "...", "document_type": "DRIW-Appointment of Alternate Director", "document_date": "2021-12-27"}}, ...]

Constraints:
- First segment starts at page 1, last segment ends at page {page_count}
- No gaps or overlaps between segments
- Return ONLY the JSON array, no other text"""


def _extract_json_array(raw: str) -> str | None:
    """Extract a JSON array from the AI response, repairing truncation if needed."""
    # Try exact match first
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        return m.group()

    # Response may be truncated (no closing ']'). Try to repair.
    m = re.search(r"\[", raw)
    if not m:
        return None

    text = raw[m.start():]
    # Find the last complete JSON object (ending with '}')
    last_brace = text.rfind("}")
    if last_brace == -1:
        return None

    repaired = text[: last_brace + 1].rstrip().rstrip(",") + "\n]"
    try:
        json.loads(repaired)
        logger.warning("Repaired truncated JSON array (%d chars recovered)", len(repaired))
        return repaired
    except json.JSONDecodeError:
        return None


def _build_page_text_block(page_texts: list[str], max_chars_per_page: int = 500) -> str:
    """Format per-page texts for the AI prompt.

    Includes the start (headers/titles) and end (dates/signatures) of each page.
    For large documents (>20 pages), reduces per-page text to fit model context.
    """
    # Scale down per-page text for large docs to avoid exceeding model output limit
    if len(page_texts) > 25:
        max_chars_per_page = 300
    elif len(page_texts) > 15:
        max_chars_per_page = 400

    parts = []
    for i, text in enumerate(page_texts):
        if len(text) <= max_chars_per_page:
            content = text
        else:
            # Show start (headers) and end (dates/signatures)
            tail = min(200, max_chars_per_page // 2)
            head = max_chars_per_page - tail
            content = text[:head] + "\n...\n" + text[-tail:]
        parts.append(f"--- PAGE {i + 1} ---\n{content}")
    return "\n\n".join(parts)


def _detect_boundaries(page_texts: list[str]) -> list[DocumentSegment]:
    """Send page texts to Ollama to identify document boundaries."""
    page_block = _build_page_text_block(page_texts)
    prompt = _SPLIT_PROMPT.format(
        page_count=len(page_texts),
        page_texts=page_block,
    )

    logger.info("Sending %d pages to AI for boundary detection...", len(page_texts))

    try:
        response = ollama_client.chat(
            model=config.OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.1, "num_predict": 4096},
        )
        raw = response["message"]["content"].strip()
    except Exception:
        logger.exception("Ollama request failed")
        return []

    # Parse JSON from response (handle markdown code blocks and truncation)
    json_text = _extract_json_array(raw)
    if not json_text:
        logger.error("AI response does not contain a JSON array:\n%s", raw[:500])
        return []

    try:
        segments_raw = json.loads(json_text)
    except json.JSONDecodeError:
        logger.exception("Failed to parse AI JSON response:\n%s", raw[:500])
        return []

    segments: list[DocumentSegment] = []
    page_count = len(page_texts)
    for item in segments_raw:
        seg = DocumentSegment(
            page_start=int(item.get("page_start", 0)),
            page_end=int(item.get("page_end", 0)),
            company_name=item.get("company_name"),
            document_type=_normalize_doc_type(item.get("document_type")),
            document_date=_normalize_date(item.get("document_date")),
        )
        # Clamp to actual page count
        seg.page_start = max(1, min(seg.page_start, page_count))
        seg.page_end = max(1, min(seg.page_end, page_count))
        segments.append(seg)

    # Sort by page_start
    segments.sort(key=lambda s: s.page_start)

    # Validate: no gaps, no overlaps
    # Fix last segment to cover remaining pages
    if segments and segments[-1].page_end < page_count:
        logger.warning(
            "Last segment ends at page %d but PDF has %d pages — extending.",
            segments[-1].page_end, page_count,
        )
        segments[-1].page_end = page_count

    # Remove segments that are fully beyond the page count (hallucinated)
    segments = [s for s in segments if s.page_start <= page_count]

    for i, seg in enumerate(segments):
        if seg.page_start < 1 or seg.page_end < seg.page_start:
            logger.warning("Invalid segment: pages %d-%d", seg.page_start, seg.page_end)
        if i > 0 and seg.page_start != segments[i - 1].page_end + 1:
            logger.warning(
                "Page gap/overlap between segments %d-%d and %d-%d",
                segments[i - 1].page_start, segments[i - 1].page_end,
                seg.page_start, seg.page_end,
            )

    return segments


def _normalize_doc_type(doc_type: str | None) -> str | None:
    """Normalize document type to Title Case, preserving known prefixes/codes."""
    if not doc_type:
        return None

    # Words that should stay lowercase (unless first word)
    _LOWERCASE_WORDS = {"of", "the", "in", "for", "and", "or", "to", "a", "an"}
    # Acronyms/codes that should stay uppercase
    _UPPERCASE_WORDS = {"AGM", "EGM", "ACRA", "DRIW", "FY", "BODM", "DPO"}

    # Split on first dash to preserve prefix like "DRIW-"
    if "-" in doc_type:
        prefix, rest = doc_type.split("-", 1)
        prefix = prefix.upper()
        parts = rest.split()
        titled = []
        for i, word in enumerate(parts):
            upper = word.upper()
            if re.match(r"^FY\d", word, re.IGNORECASE):
                titled.append(upper)
            elif upper in _UPPERCASE_WORDS:
                titled.append(upper)
            elif word.lower() in _LOWERCASE_WORDS and i > 0:
                titled.append(word.lower())
            elif word.isupper() and len(word) > 1:
                titled.append(word.title())
            else:
                titled.append(word)
        return f"{prefix}-{' '.join(titled)}"

    return doc_type


def _normalize_date(date_str: str | None) -> str | None:
    """Normalize a date string to YYYY-MM-DD."""
    if not date_str:
        return None

    # Already in YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str

    # Try DD/MM/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", date_str)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

    return date_str


# ---------------------------------------------------------------------------
# PDF splitting
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """Remove characters that are invalid in Windows filenames."""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def _generate_segment_filename(seg: DocumentSegment) -> str | None:
    """Generate a standardized filename for a document segment.

    Format: YYYYMMDD ABBR-DocType.pdf
    """
    abbr = seg.abbreviation
    if not abbr or abbr == "UNKNOWN":
        logger.error(
            "Company not found in Entity List for segment pages %d-%d: %s",
            seg.page_start, seg.page_end, seg.company_name,
        )
        return None

    date_part = ""
    if seg.document_date:
        date_part = seg.document_date.replace("-", "")

    doc_type = seg.document_type or "Unknown Document"
    doc_type = _sanitize_filename(doc_type)

    filename = f"{date_part} {abbr}-{doc_type}.pdf"
    return filename


def _split_pdf_by_segments(
    pdf_path: Path,
    segments: list[DocumentSegment],
    output_folder: Path,
) -> list[Path]:
    """Split a PDF into individual files based on detected segments.

    Returns list of created file paths.
    """
    output_folder.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    created: list[Path] = []

    for seg in segments:
        filename = _generate_segment_filename(seg)
        if not filename:
            logger.warning(
                "Skipping segment pages %d-%d (no valid filename)",
                seg.page_start, seg.page_end,
            )
            continue

        dest = output_folder / filename

        # Handle filename collisions
        counter = 1
        while dest.exists():
            stem = dest.stem
            dest = output_folder / f"{stem} ({counter}).pdf"
            counter += 1

        # Extract page range (fitz uses 0-based indices)
        new_doc = fitz.open()
        new_doc.insert_pdf(
            doc,
            from_page=seg.page_start - 1,
            to_page=seg.page_end - 1,
        )
        new_doc.save(str(dest))
        new_doc.close()

        logger.info(
            "  Created: %s (pages %d-%d)",
            dest.name, seg.page_start, seg.page_end,
        )
        created.append(dest)

    doc.close()
    return created


# ---------------------------------------------------------------------------
# Interactive review
# ---------------------------------------------------------------------------

def _interactive_review(
    segments: list[DocumentSegment], total_pages: int,
) -> list[DocumentSegment] | None:
    """Show detected segments and let the user confirm, edit, or cancel.

    Returns updated segments list, or None if cancelled.
    """
    print("\n" + "=" * 70)
    print("  AI-detected document segments (review before splitting)")
    print("=" * 70)

    for i, seg in enumerate(segments):
        abbr = seg.abbreviation or "???"
        print(
            f"  {i + 1}. Pages {seg.page_start:>2}-{seg.page_end:<2}  |  "
            f"{seg.document_date or 'no-date'}  |  {abbr}  |  "
            f"{seg.document_type or 'unknown'}"
        )

    print("=" * 70)
    print("  Options:")
    print("    Enter  = Accept and split")
    print("    e      = Edit a segment")
    print("    d      = Delete a segment (merge into previous)")
    print("    c      = Cancel")
    print("=" * 70)

    while True:
        choice = input("\n  Choice: ").strip().lower()

        if choice == "" or choice == "y":
            return segments

        elif choice == "c":
            return None

        elif choice == "e":
            segments = _edit_segment(segments, total_pages)
            # Re-display
            print()
            for i, seg in enumerate(segments):
                abbr = seg.abbreviation or "???"
                print(
                    f"  {i + 1}. Pages {seg.page_start:>2}-{seg.page_end:<2}  |  "
                    f"{seg.document_date or 'no-date'}  |  {abbr}  |  "
                    f"{seg.document_type or 'unknown'}"
                )

        elif choice == "d":
            segments = _delete_segment(segments, total_pages)
            # Re-display
            print()
            for i, seg in enumerate(segments):
                abbr = seg.abbreviation or "???"
                print(
                    f"  {i + 1}. Pages {seg.page_start:>2}-{seg.page_end:<2}  |  "
                    f"{seg.document_date or 'no-date'}  |  {abbr}  |  "
                    f"{seg.document_type or 'unknown'}"
                )

        else:
            print("  Invalid choice. Press Enter to accept, 'e' to edit, 'd' to delete, 'c' to cancel.")


def _edit_segment(
    segments: list[DocumentSegment], total_pages: int,
) -> list[DocumentSegment]:
    """Edit one segment's fields interactively."""
    try:
        idx = int(input("  Segment # to edit: ").strip()) - 1
        if idx < 0 or idx >= len(segments):
            print("  Invalid segment number.")
            return segments
    except ValueError:
        print("  Invalid input.")
        return segments

    seg = segments[idx]
    print(f"  Editing segment {idx + 1}: Pages {seg.page_start}-{seg.page_end}")
    print("  (Press Enter to keep current value)\n")

    val = input(f"    page_start [{seg.page_start}]: ").strip()
    if val:
        seg.page_start = int(val)

    val = input(f"    page_end [{seg.page_end}]: ").strip()
    if val:
        seg.page_end = int(val)

    val = input(f"    document_type [{seg.document_type}]: ").strip()
    if val:
        seg.document_type = val

    val = input(f"    document_date [{seg.document_date}]: ").strip()
    if val:
        seg.document_date = _normalize_date(val)

    val = input(f"    company_name [{seg.company_name}]: ").strip()
    if val:
        seg.company_name = val

    # Re-sort and fix adjacent boundaries
    segments.sort(key=lambda s: s.page_start)
    return segments


def _delete_segment(
    segments: list[DocumentSegment], total_pages: int,
) -> list[DocumentSegment]:
    """Delete a segment and merge its pages into the previous one."""
    try:
        idx = int(input("  Segment # to delete (merges into previous): ").strip()) - 1
        if idx < 0 or idx >= len(segments):
            print("  Invalid segment number.")
            return segments
    except ValueError:
        print("  Invalid input.")
        return segments

    if idx == 0 and len(segments) > 1:
        # Merge into next
        segments[1].page_start = segments[0].page_start
        segments.pop(0)
    elif idx > 0:
        # Merge into previous
        segments[idx - 1].page_end = segments[idx].page_end
        segments.pop(idx)
    else:
        print("  Cannot delete the only segment.")

    return segments


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def split_pdf(pdf_path: Path, output_folder: Path | None = None) -> list[Path]:
    """Split a combined PDF into individual documents.

    Steps:
      1. OCR each page individually
      2. Send page texts to AI for document boundary detection
      3. Split PDF and save individual files with standardized names

    Args:
        pdf_path: Path to the combined PDF.
        output_folder: Where to save split files.
                       Defaults to config.RENAMED_FOLDER.

    Returns:
        List of paths to created files.
    """
    if output_folder is None:
        output_folder = config.RENAMED_FOLDER

    logger.info("Splitting PDF: %s (%s)", pdf_path.name, pdf_path)

    # Step 1: Per-page OCR
    logger.info("[Split 1] Running per-page OCR...")
    page_texts = _ocr_pages(pdf_path)
    logger.info("[Split 1] OCR complete — %d pages extracted.", len(page_texts))

    if not page_texts:
        logger.error("No text extracted from any page.")
        return []

    # Log brief preview per page
    for i, text in enumerate(page_texts):
        preview = text[:80].replace("\n", " ")
        logger.debug("  Page %d (%d chars): %s", i + 1, len(text), preview)

    # Step 2: AI boundary detection
    logger.info("[Split 2] Detecting document boundaries with AI...")
    segments = _detect_boundaries(page_texts)

    if not segments:
        logger.error("AI could not identify any document segments.")
        return []

    logger.info("[Split 2] Detected %d document(s):", len(segments))
    for i, seg in enumerate(segments):
        logger.info(
            "  %d. Pages %d-%d | %s | %s | %s",
            i + 1, seg.page_start, seg.page_end,
            seg.document_date or "no-date",
            seg.abbreviation or "???",
            seg.document_type or "unknown",
        )

    # Step 2b: Interactive review — let user confirm or adjust
    segments = _interactive_review(segments, len(page_texts))

    if not segments:
        logger.info("Split cancelled by user.")
        return []

    # Step 3: Split PDF
    logger.info("[Split 3] Splitting PDF into %d files...", len(segments))
    created = _split_pdf_by_segments(pdf_path, segments, output_folder)
    logger.info("[Split 3] Done — %d file(s) created.", len(created))

    return created
