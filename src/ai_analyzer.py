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
  "document_content": "A brief summary of the document's subject matter or purpose (e.g. 'Opening of Bank Accounts with DBS Bank', 'Appointment of John Tan as Director', 'Allotment of 100,000 ordinary shares'). This should describe WHAT the document is about, not its type.",
  "document_date": "The primary date of the document in YYYY-MM-DD format. Look for dates near the top of the document, signature dates, or resolution dates. If multiple dates exist, use the main document date, not filing dates.",
  "confidence": "high/medium/low - your confidence in the extraction accuracy",
  "position_held": "If this is an ACRA 'Change in Company Information' document about appointment/cessation of officers or auditors, extract the 'Position held' value (e.g. 'Director', 'Secretary', 'Auditor'). Otherwise null.",
  "has_appointment_date": "true if a 'Date of Appointment' field with a date value is found, false otherwise",
  "has_cessation_date": "true if a 'Date of Cessation' field with a date value is found, false otherwise",
  "has_cessation_reason": "true if a 'Reason of Cessation' field with a value is found, false otherwise",
  "entry_indicators": "If you see patterns like '[1/2]', '[2/2]' or 'Appointment or Cessation of Company Officers or Auditors [1/2]', return the total count (e.g. 2). Otherwise null."
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
- For position_held: Only extract for ACRA officer change documents. Common values: "Director", "Secretary", "Auditor", "Managing Director", "Chief Executive Officer"
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
    document_content: str | None  # Subject/purpose of the document
    document_date: str | None  # YYYY-MM-DD format
    confidence: str
    # ACRA Change-of-Officers specific fields
    position_held: str | None = None        # e.g. "Director", "Secretary", "Auditor"
    has_cessation: bool = False              # True if cessation date/reason found
    is_multi_entry: bool = False             # True if multiple entries e.g. [1/2] [2/2]
    all_cessation: bool = False             # True if ALL entries are cessations (no appointment-only)


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

        metadata = _parse_response(raw_response)

        # Validate: check if AI company name actually appears in the OCR text
        _validate_company_name(metadata, text)

        # Supplement with OCR text-based detection for ACRA officer-change docs
        _enrich_officer_change_fields(metadata, text)

        return metadata

    except Exception:
        logger.exception("Ollama analysis failed")
        return DocumentMetadata(
            company_name=None,
            document_type=None,
            document_content=None,
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


def _to_bool(val) -> bool:
    """Coerce various truthy representations to bool."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "1")
    return bool(val)


# ---------- Company name validation ----------

def _strip_entity_suffixes(name: str) -> str:
    """Remove common entity suffixes (Pte, Ltd, etc.) and normalise whitespace."""
    result = name.lower()
    for suffix in ("pte", "ltd", "pte.", "ltd.", "sdn", "bhd",
                    "sdn.", "bhd.", "limited", "private"):
        result = result.replace(suffix, "")
    return " ".join(result.split()).strip()


# Regex to find "COMPANY NAME PTE. LTD." patterns in OCR text.
# Requires each word to start with uppercase (no IGNORECASE) and
# uses word-boundary to avoid partial matches like "c/o".
_COMPANY_NAME_PATTERN = re.compile(
    r"(?<!\w)"                                          # not preceded by a word char
    r"((?:[A-Z][A-Z0-9.&'\-]*\s+){1,8})"              # 1-8 uppercase words
    r"(?:PTE|PRIVATE)\.?\s*(?:LTD|LIMITED)\.?"          # entity suffix
)


# Common English words that precede company names but aren't part of them
_NOISE_PREFIXES = {
    "THE", "OF", "IN", "FOR", "BY", "TO", "AND", "OR", "WITH", "AT", "ON",
    "COMPANY", "INTEREST", "NAME", "ITS", "THEIR", "THIS", "THAT", "FROM",
    "BETWEEN", "BEING", "SAID", "CALLED", "KNOWN", "AS", "IS", "WAS",
    "SECRETARY", "DIRECTOR", "DIRECTORS", "MEMBER", "MEMBERS",
    "PURSUANT", "SECTION", "UNDER", "HEREINAFTER", "ENTITLED",
}


def _clean_company_match(raw: str) -> str:
    """Strip leading noise/generic words from a regex-captured company name."""
    words = raw.strip().split()
    while words and words[0].upper().rstrip(".,") in _NOISE_PREFIXES:
        words.pop(0)
    return " ".join(words).strip()


def _validate_company_name(metadata: DocumentMetadata, ocr_text: str) -> None:
    """Verify the AI-extracted company name actually appears in the OCR text.

    If the AI hallucinated a company name not found in the text, attempt to find
    the real company by:
      1. Checking known companies from COMPANY_SHORT_NAMES config
      2. Extracting company names directly from OCR text via regex (most frequent)
    """
    if not metadata.company_name:
        return

    text_lower = ocr_text.lower()
    name_core = _strip_entity_suffixes(metadata.company_name)

    if name_core and name_core in text_lower:
        return  # AI name is present in OCR text — all good

    logger.warning(
        "AI company name '%s' NOT found in OCR text — likely hallucinated.",
        metadata.company_name,
    )

    # Strategy 1: Try to find a known company from config in the OCR text
    for full_name in config.COMPANY_SHORT_NAMES:
        known_core = _strip_entity_suffixes(full_name)
        if known_core and known_core in text_lower:
            corrected = config.get_company_full_name(
                config.get_company_short_name(full_name)
            )
            if corrected:
                logger.info(
                    "Corrected company: '%s' → '%s' (config match in OCR text)",
                    metadata.company_name, corrected,
                )
                metadata.company_name = corrected
                return

    # Strategy 2: Extract company names from OCR text via regex, pick most frequent
    raw_matches = _COMPANY_NAME_PATTERN.findall(ocr_text)
    if raw_matches:
        from collections import Counter
        counts: Counter[str] = Counter()
        core_to_clean: dict[str, str] = {}
        for raw_match in raw_matches:
            cleaned = _clean_company_match(raw_match)
            if not cleaned:
                continue
            cleaned_core = _strip_entity_suffixes(cleaned)
            if cleaned_core and cleaned_core != name_core:
                counts[cleaned_core] += 1
                # Keep the longest cleaned form for display
                if cleaned_core not in core_to_clean or len(cleaned) > len(core_to_clean[cleaned_core]):
                    core_to_clean[cleaned_core] = cleaned

        if counts:
            best_core = counts.most_common(1)[0][0]
            best_clean = core_to_clean[best_core]
            corrected = best_clean.strip().title() + " Pte Ltd"
            logger.info(
                "Corrected company: '%s' → '%s' (most frequent in OCR text, %dx)",
                metadata.company_name, corrected, counts[best_core],
            )
            metadata.company_name = corrected
            return

    logger.warning("Could not find any company name in OCR text — keeping AI result.")


# ---------- ACRA officer-change OCR-based detection ----------

_OFFICER_CHANGE_PATTERN = re.compile(
    r"change\s+in\s+company\s+information.*?appointment.*?cessation.*?officer|auditor",
    re.IGNORECASE | re.DOTALL,
)

_POSITION_PATTERN = re.compile(
    # ACRA two-column layout: "Position held" is a header, value is on the next line
    # e.g.: "Position held        Date of Appointment\nSecretary       26/04/2024"
    r"position\s+held\b[^\n]*\n\s*([A-Za-z][\w\s]*?)(?:\s{3,}|\t|\n|$)",
    re.IGNORECASE,
)

_APPT_DATE_PATTERN = re.compile(
    r"date\s+of\s+appointment\s*[:\-]?\s*(\S.+)",
    re.IGNORECASE,
)

_CESS_DATE_PATTERN = re.compile(
    r"date\s+of\s+cessation\s*[:\-]?\s*(\S.+)",
    re.IGNORECASE,
)

_CESS_REASON_PATTERN = re.compile(
    r"reason\s+(?:of|for)\s+cessation\s*[:\-]?\s*(\S.+)",
    re.IGNORECASE,
)

_ENTRY_COUNT_PATTERN = re.compile(
    r"\[(\d+)/(\d+)\]",
)

_CURRENT_ENTITY_PATTERN = re.compile(
    r"current\s+entity\s+details",
    re.IGNORECASE,
)


def _enrich_officer_change_fields(metadata: DocumentMetadata, ocr_text: str) -> None:
    """Scan OCR text directly for ACRA officer-change fields.

    This supplements the AI extraction with reliable regex-based detection,
    since the AI may miss these structured fields.
    Everything after "Current Entity Details" is ignored.
    """
    # Only apply to ACRA officer-change documents
    is_officer_change = (
        _OFFICER_CHANGE_PATTERN.search(ocr_text)
        or (metadata.document_content and "appointment" in metadata.document_content.lower()
            and "cessation" in metadata.document_content.lower())
        or (metadata.document_type and "appointment" in metadata.document_type.lower()
            and ("officer" in metadata.document_type.lower() or "auditor" in metadata.document_type.lower()))
    )

    if not is_officer_change:
        return

    logger.debug("Detected ACRA officer-change document — scanning OCR text for fields.")

    # Strip everything after "Current Entity Details" — those fields are irrelevant
    ced_match = _CURRENT_ENTITY_PATTERN.search(ocr_text)
    scan_text = ocr_text[:ced_match.start()] if ced_match else ocr_text

    # Position held — use only the first match from OCR (first entry is the primary change)
    if not metadata.position_held or not isinstance(metadata.position_held, str):
        m = _POSITION_PATTERN.search(scan_text)
        if m:
            position = m.group(1).strip().rstrip(".,;:")
            position = re.sub(r"\s+", " ", position)
            position = position.split("\n")[0].strip()
            if len(position) <= 60:
                metadata.position_held = position
                logger.info("OCR detected position_held: '%s'", position)

    # Date of Appointment
    has_appt = bool(_APPT_DATE_PATTERN.search(scan_text))

    # Count cessation dates and reasons across all entries
    cess_date_count = len(_CESS_DATE_PATTERN.findall(scan_text))
    cess_reason_count = len(_CESS_REASON_PATTERN.findall(scan_text))
    has_cess_date = cess_date_count > 0
    has_cess_reason = cess_reason_count > 0

    # Multi-entry indicators [1/2], [2/2], etc.
    entry_matches = _ENTRY_COUNT_PATTERN.findall(ocr_text)  # check full text for [x/y]
    is_multi = False
    entry_total = 1
    if entry_matches:
        entry_total = max(int(m[1]) for m in entry_matches)
        is_multi = entry_total > 1

    # Override AI results with OCR text results (OCR regex is more reliable here)
    if has_cess_date and has_cess_reason:
        metadata.has_cessation = True
    if is_multi:
        metadata.is_multi_entry = True

    # All entries are cessations if every entry has a cessation date+reason
    # (i.e. number of cessation occurrences >= number of entries)
    if cess_date_count >= entry_total and cess_reason_count >= entry_total:
        metadata.all_cessation = True

    logger.info(
        "ACRA officer-change fields — position: %s | appt_date: %s | "
        "cess_date: %s (%d) | cess_reason: %s (%d) | entries: %d | "
        "multi_entry: %s | all_cessation: %s",
        metadata.position_held, has_appt,
        has_cess_date, cess_date_count, has_cess_reason, cess_reason_count,
        entry_total, metadata.is_multi_entry, metadata.all_cessation,
    )


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
        document_content=None,
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
        return DocumentMetadata(None, None, None, None, "low")

    raw_date = data.get("document_date")

    # ACRA officer-change fields
    position_held = data.get("position_held")
    # AI may return a list of positions — use only the first one
    if isinstance(position_held, list):
        position_held = position_held[0] if position_held else None
    has_appt = _to_bool(data.get("has_appointment_date"))
    has_cess = _to_bool(data.get("has_cessation_date"))
    has_cess_reason = _to_bool(data.get("has_cessation_reason"))
    entry_count = data.get("entry_indicators")
    is_multi = (isinstance(entry_count, (int, float)) and entry_count > 1)

    # Cessation = has cessation date AND reason
    has_cessation = has_cess and has_cess_reason

    return DocumentMetadata(
        company_name=_fix_ocr_company_name(data.get("company_name")),
        document_type=data.get("document_type"),
        document_content=data.get("document_content"),
        document_date=_normalize_date(raw_date) if raw_date else None,
        confidence=data.get("confidence", "low"),
        position_held=position_held,
        has_cessation=has_cessation,
        is_multi_entry=is_multi,
    )
