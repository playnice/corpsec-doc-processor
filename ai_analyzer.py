"""
AI Analyzer module.
Uses Ollama to extract structured metadata from corporate documents:
  - Company name
  - Document type / title
  - Document date

Two analysis paths:
  - analyze_document(text)        — for digitally-created PDFs (text already extracted)
  - analyze_document_vision(path) — for scanned PDFs (sends page images to vision model)
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import ollama as ollama_client

import config

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """You are a Corporate Secretary document analyst. Analyze the following text extracted from a scanned corporate/secretarial document.

Extract the following information and return ONLY a valid JSON object (no markdown, no explanation):

{{
  "company_name": "Full registered company name as it appears in the document",
  "document_type": "The type/title of the document (e.g. 'Authority to Issue Shares', 'Board Resolution', 'Annual Return', 'Notice of AGM', 'Certificate of Incorporation', 'Share Transfer Form', 'Directors Resolution in Writing', 'Memorandum and Articles of Association')",
  "document_date": "The primary date of the document in YYYY-MM-DD format. Look for dates near the top of the document, signature dates, or resolution dates. If multiple dates exist, use the main document date, not filing dates.",
  "confidence": "high/medium/low - your confidence in the extraction accuracy"
}}

Rules:
- For company_name: Use the EXACT full legal name as written (e.g. "Zoo Capital Fund II Pte Ltd" not "Zoo Capital Fund 2")
- For document_type: Use a concise descriptive title in Title Case. Strip prefixes like "Form of" or "Copy of"
- For document_date: Convert any date format to YYYY-MM-DD. If only month and year, use the 1st of the month. If date is unclear, set to null.
- If a field cannot be determined, set it to null

--- DOCUMENT TEXT ---
{text}
--- END DOCUMENT TEXT ---

Return ONLY the JSON object:"""

VISION_EXTRACTION_PROMPT = """You are a Corporate Secretary document analyst. Carefully examine this scanned corporate/secretarial document image.

Extract the following information and return ONLY a valid JSON object (no markdown, no explanation):

{
  "company_name": "Full registered company name exactly as printed in the document",
  "document_type": "The type/title of the document (e.g. 'Authority to Issue Shares', 'Board Resolution', 'Annual Return', 'Notice of AGM', 'Certificate of Incorporation', 'Share Transfer Form', 'Directors Resolution in Writing', 'Memorandum and Articles of Association')",
  "document_date": "The primary date of the document in YYYY-MM-DD format. Look for dates near the top, signature dates, or resolution dates. If multiple dates exist, use the main document date, not filing dates.",
  "confidence": "high/medium/low - your confidence in the extraction accuracy"
}

CRITICAL reading rules — pay close attention to visually similar characters:
- Letter Z vs digit 2  (e.g. "ZOO" not "200", "ZERO" not "2ERO")
- Letter O vs digit 0
- Letter I vs digit 1
- Letter B vs digit 8
- Letter S vs digit 5
- For company_name: Copy the EXACT legal name as printed, including "Pte. Ltd.", "Pte Ltd", "Sdn Bhd", "Ltd", etc.
- For document_type: Concise descriptive title in Title Case. Strip prefixes like "Form of" or "Copy of"
- For document_date: Convert any date format to YYYY-MM-DD. If only month/year, use the 1st. If unclear, set to null.
- If a field cannot be determined, set it to null

Return ONLY the JSON object:"""


@dataclass
class DocumentMetadata:
    """Structured metadata extracted from a document."""
    company_name: str | None
    document_type: str | None
    document_date: str | None  # YYYY-MM-DD format
    confidence: str


def analyze_document(text: str) -> DocumentMetadata:
    """
    Send extracted document text to Ollama Llama3 for metadata extraction.

    Args:
        text: The OCR-extracted text from the document.

    Returns:
        DocumentMetadata with extracted fields.
    """
    # Truncate very long documents to avoid context window issues
    max_chars = 4000
    if len(text) > max_chars:
        # Keep first and last portions for context
        text = text[:3000] + "\n...[truncated]...\n" + text[-1000:]

    prompt = EXTRACTION_PROMPT.format(text=text)

    try:
        response = ollama_client.chat(
            model=config.OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            format="json",
            options={"temperature": 0.1},  # Low temperature for deterministic output
        )

        raw_response = response["message"]["content"].strip()
        logger.debug("Ollama raw response: %s", raw_response)

        return _parse_response(raw_response)

    except Exception:
        logger.exception("Ollama analysis failed")
        return DocumentMetadata(
            company_name=None,
            document_type=None,
            document_date=None,
            confidence="low",
        )


def analyze_document_vision(pdf_path: Path) -> DocumentMetadata:
    """
    Analyze a scanned PDF by merging all pages into one image and sending it
    to an Ollama vision model in a single request.  This gives the model the
    full document context (signatures, dates, company names on any page).

    Args:
        pdf_path: Path to the scanned PDF file.

    Returns:
        DocumentMetadata with extracted fields.
    """
    from ocr_engine import merge_pages_to_image

    merged_image = merge_pages_to_image(pdf_path)
    if not merged_image:
        logger.error("Could not render pages from %s", pdf_path.name)
        return DocumentMetadata(None, None, None, "low")

    logger.info("Sending merged document image to vision model %s...", config.OLLAMA_VISION_MODEL)
    try:
        response = ollama_client.chat(
            model=config.OLLAMA_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": VISION_EXTRACTION_PROMPT,
                "images": [merged_image],
            }],
            format="json",
            options={"temperature": 0.1},
        )
        raw_response = response["message"]["content"].strip()
        logger.debug("Ollama vision raw response: %s", raw_response)
        return _parse_response(raw_response)

    except Exception:
        logger.exception("Ollama vision analysis failed")
        return DocumentMetadata(None, None, None, "low")


def _normalize_date(date_str: str) -> str | None:
    """Convert various date formats to YYYY-MM-DD."""
    if not date_str:
        return None
    date_str = date_str.strip().strip("*").strip()
    formats = [
        "%Y-%m-%d",   # already correct
        "%d/%m/%Y",   # 27/06/2024  (Singapore/UK style)
        "%d-%m-%Y",   # 27-06-2024
        "%d %B %Y",   # 27 June 2024
        "%d %b %Y",   # 27 Jun 2024
        "%B %d, %Y",  # June 27, 2024
        "%b %d, %Y",  # Jun 27, 2024
        "%d/%m/%y",   # 27/06/24
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    logger.debug("Could not parse date string: %s", date_str)
    return None


def _parse_markdown_response(text: str) -> DocumentMetadata | None:
    """
    Fallback parser for when the model ignores the JSON instruction and
    returns markdown bullet points instead (e.g. '* **Company Name**: Foo').
    """
    def extract(pattern: str) -> str | None:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip().strip("*").strip() if m else None

    company = extract(r'\*{0,2}Company Name\*{0,2}\s*[:\-]\s*(.+)')
    doc_type = extract(r'\*{0,2}Document Type\*{0,2}\s*[:\-]\s*(.+)')
    raw_date = extract(r'\*{0,2}Document Date\*{0,2}\s*[:\-]\s*(.+)')
    raw_conf = extract(r'\*{0,2}Confidence\*{0,2}\s*[:\-]\s*(\w+)')

    if not any([company, doc_type, raw_date]):
        return None

    conf = raw_conf.lower() if raw_conf and raw_conf.lower() in ("high", "medium", "low") else "medium"
    return DocumentMetadata(
        company_name=company,
        document_type=doc_type,
        document_date=_normalize_date(raw_date) if raw_date else None,
        confidence=conf,
    )


def _parse_response(raw: str) -> DocumentMetadata:
    """Parse the JSON response from Ollama into DocumentMetadata."""
    # Strip markdown code fences if the model wraps output
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    data = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find a JSON object anywhere in the response
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                pass

    if data is None:
        # Last resort: parse markdown bullet-point format the model sometimes returns
        md_result = _parse_markdown_response(raw)
        if md_result:
            logger.warning("Parsed response as markdown (model ignored JSON instruction)")
            return md_result
        logger.error("Could not parse Ollama response: %s", raw[:300])
        return DocumentMetadata(None, None, None, "low")

    raw_date = data.get("document_date")
    return DocumentMetadata(
        company_name=data.get("company_name"),
        document_type=data.get("document_type"),
        document_date=_normalize_date(raw_date) if raw_date else None,
        confidence=data.get("confidence", "low"),
    )
