"""
AI Analyzer module.
Uses Ollama (Llama3) to extract structured metadata from document text:
  - Company name
  - Document type / title
  - Document date
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

import ollama as ollama_client

import config

logger = logging.getLogger(__name__)

# ---------- OCR company-name correction ----------
_DIGIT_TO_LETTER = {'0': 'O', '1': 'I', '2': 'Z', '5': 'S', '8': 'B'}
_ROMAN_NUMERALS = {'I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X', 'XI', 'XII'}
_CORP_SUFFIXES_SET = {'PTE', 'LTD', 'SDN', 'BHD', 'INC', 'CORP', 'LIMITED', 'PRIVATE', 'COMPANY', 'CO'}


def _apply_digit_to_letter(name: str) -> str:
    """Convert digit→letter in every token of a company name (helper)."""
    words = name.split()
    fixed = []
    for word in words:
        core = word.rstrip('.,;:()')
        tail = word[len(core):]
        upper = core.upper()

        if upper in _CORP_SUFFIXES_SET or upper in _ROMAN_NUMERALS:
            fixed.append(word)
            continue

        if core.isdigit() and len(core) >= 2:
            fixed.append(''.join(_DIGIT_TO_LETTER.get(c, c) for c in core) + tail)
        elif any(c.isdigit() for c in core) and any(c.isalpha() for c in core):
            fixed.append(''.join(
                _DIGIT_TO_LETTER.get(c, c) if c.isdigit() else c for c in core
            ) + tail)
        else:
            fixed.append(word)
    return ' '.join(fixed)


def _fix_ocr_company_name(name: str | None) -> str | None:
    """
    Attempt to correct OCR digit→letter confusion in a company name,
    but ONLY if the corrected version matches a known company in config.
    This avoids false-positive corrections on companies that legitimately
    have numbers in their name (e.g. "88 Holdings Pte Ltd").
    """
    if not name:
        return name

    corrected = _apply_digit_to_letter(name)
    if corrected == name:
        return name  # no digits to fix

    # Check if the corrected name matches a known company
    corrected_lower = corrected.strip().lower()
    for key in config.COMPANY_SHORT_NAMES:
        if key in corrected_lower or corrected_lower in key:
            logger.info("OCR company name corrected: '%s' → '%s'", name, corrected)
            return corrected

    # Corrected name doesn't match any known company — keep original
    return name

EXTRACTION_PROMPT = """You are a Corporate Secretary document analyst. Analyze the following text extracted via OCR from a scanned corporate/secretarial document.

Extract the following information and return ONLY a valid JSON object (no markdown, no explanation):

{{
  "company_name": "Full registered company name as it appears in the document",
  "document_type": "The type/title of the document (e.g. 'Authority to Issue Shares', 'Board Resolution', 'Annual Return', 'Notice of AGM', 'Certificate of Incorporation', 'Share Transfer Form', 'Directors Resolution in Writing', 'Memorandum and Articles of Association')",
  "document_date": "The primary date of the document in YYYY-MM-DD format. Look for dates near the top of the document, signature dates, or resolution dates. If multiple dates exist, use the main document date, not filing dates.",
  "confidence": "high/medium/low - your confidence in the extraction accuracy"
}}

IMPORTANT — This text comes from OCR which often confuses visually similar characters.
Apply these corrections when interpreting company names and other proper nouns:
- "200" in a company name is almost certainly "ZOO" (Z misread as 2, O misread as 0)
- "0" in a name/word context is likely "O" (letter, not digit)
- "1" in a name/word context is likely "I" or "l"
- "5" at the start of a word is likely "S"
- "8" in a name context is likely "B"
Use surrounding context to decide: if the text looks like a company name or English word, prefer letters over digits.

Rules:
- For company_name: Use the corrected full legal name (e.g. "Zoo Capital Fund II Pte Ltd" not "200 Capital Fund II Pte Ltd")
- For document_type: Use a concise descriptive title in Title Case. Strip prefixes like "Form of" or "Copy of"
- For document_date: Convert any date format to YYYY-MM-DD. If only month and year, use the 1st of the month. If date is unclear, set to null.
- If a field cannot be determined, set it to null

--- DOCUMENT TEXT ---
{text}
--- END DOCUMENT TEXT ---

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
        text = text[:3000] + "\n...[truncated]...\n" + text[-1000:]

    prompt = EXTRACTION_PROMPT.format(text=text)

    try:
        response = ollama_client.chat(
            model=config.OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            format="json",
            options={"temperature": 0.1},
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
    returns markdown bullet points instead.
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
        company_name=_fix_ocr_company_name(company),
        document_type=doc_type,
        document_date=_normalize_date(raw_date) if raw_date else None,
        confidence=conf,
    )


def _parse_response(raw: str) -> DocumentMetadata:
    """Parse the JSON response from Ollama into DocumentMetadata."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    data = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                pass

    if data is None:
        md_result = _parse_markdown_response(raw)
        if md_result:
            logger.warning("Parsed response as markdown (model ignored JSON instruction)")
            return md_result
        logger.error("Could not parse Ollama response: %s", raw[:300])
        return DocumentMetadata(None, None, None, "low")

    raw_date = data.get("document_date")
    return DocumentMetadata(
        company_name=_fix_ocr_company_name(data.get("company_name")),
        document_type=data.get("document_type"),
        document_date=_normalize_date(raw_date) if raw_date else None,
        confidence=data.get("confidence", "low"),
    )
