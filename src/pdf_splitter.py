"""
PDF Splitter module.
Splits combined/multi-document scanned PDFs into individual documents.

Uses per-page OCR + heuristic pattern matching on resolution headers to
identify document boundaries, then splits using PyMuPDF and names each
output file using Entity List CSV.
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
# Heuristic boundary detection (regex-based, primary method)
# ---------------------------------------------------------------------------

# Resolution header pattern — handles OCR artifacts (Æ for ', * for ')
_RESOLUTION_RE = re.compile(
    r"DIRECTOR.{0,6}S?.{0,6}\s+RESOLUTION.{0,6}S?\s+IN\s+WRITING",
    re.IGNORECASE,
)

# Continuation page indicator (e.g. "Page 2 of 4", "Page 3")
_PAGE_NUM_RE = re.compile(r"\bPage\s+(\d+)\b", re.IGNORECASE)

# Pages that are always continuations (supporting docs, not new resolutions)
_CONTINUATION_START_RE = re.compile(
    r"^(?:SCHEDULE\b|This\s+is\s+the\s+Schedule|APPLICATION\s+FOR\s+SHARE|"
    r"WRITTEN\s+NOTICE\s+OF\s+DIRECTOR|Signature\s+Page|"
    r"First\s+Board\s+Resolutions?)",
    re.IGNORECASE,
)

# Company name — first line containing PTE LTD / LIMITED etc.
# Only search the first ~200 chars (first few lines) to avoid matching bank names
_COMPANY_RE = re.compile(
    r"^(.+?(?:PTE\.?\s*LTD\.?|PRIVATE\s+LIMITED))",
    re.IGNORECASE | re.MULTILINE,
)

# Action keywords that appear in subject lines
_ACTION_KEYWORDS = [
    "APPOINTMENT", "ALLOTMENT", "ISSUANCE", "DECLARATION",
    "OPENING", "CONVENING", "INCORPORATION", "CHANGE",
    "AUTHORITY", "RESIGNATION", "AGM", "ANNUAL GENERAL",
    "APPROVAL", "REGISTERED OFFICE", "FINANCIAL YEAR",
    "BANK ACCOUNT", "ALTERNATE DIRECTOR", "AUDITOR",
    "NOTICE OF RESOLUTION", "LETTER OF AUTHORITY",
]

# Lines that are NOT subjects (boilerplate in resolution headers)
_BOILERPLATE_RE = re.compile(
    r"^(?:DIRECTOR.{0,10}RESOLUTION|"  # "Directors' Resolution(s)..." header line
    r"PURSUANT|CONSTITUTION|THE\s+COMPAN|COMPAN[YÆ']|"
    r"INCORPORATED|REPUBLIC\s+OF|REGISTRATION|RESOLVED|THAT\s+|"
    r"DATE\s*[:\-]|Page\s+\d|Signature\s+Page|Company\s+No)",
    re.IGNORECASE,
)

# Month abbreviation → number
_MONTH_MAP = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
    "JANUARY": "01", "FEBRUARY": "02", "MARCH": "03", "APRIL": "04",
    "JUNE": "06", "JULY": "07", "AUGUST": "08", "SEPTEMBER": "09",
    "OCTOBER": "10", "NOVEMBER": "11", "DECEMBER": "12",
}

# OCR-mangled month variants (e.g. JNN→JUN, AUG with ! for 1 in year)
_MONTH_FUZZY: dict[str, str] = {
    "JNN": "06", "JUM": "06", "JUH": "06",
    "JAM": "01", "JAN": "01",
    "FE8": "02",
    "AUC": "08", "AUG": "08",
    "0CT": "10",
    "NOY": "11",
    "DFC": "12", "DEC": "12",
}

# Subject normalization: raw OCR subject → clean document type
_SUBJECT_NORMALIZATIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"APPOINTMENT\s+OF\s+ALTERNATE\s+DIRECTOR", re.I), "Appointment of Alternate Director"),
    (re.compile(r"APPOINTMENT\s+OF\s+AUDITOR", re.I), "Appointment of Auditors"),
    (re.compile(r"APPOINTMENT\s+OF\s+(?:DATA\s+PROTECTION\s+OFFICER|DPO)", re.I), "Appointment of DPO"),
    (re.compile(r"APPOINTMENT\s+OF\s+DIRECTOR", re.I), "Appointment of Directors"),
    (re.compile(r"ALLOTMENT\s+(?:AND\s+ISSUANCE\s+)?OF\b.*?SHARES", re.I), "Allotment of Shares"),
    (re.compile(r"DECLARATION\s+OF\s+INTERESTS", re.I), "Declaration of Interests"),
    (re.compile(r"OPENING\s+OF\s+BANK\s+ACCOUNT.*?(?:UNITED\s+OVERSEAS|UOB)", re.I), "Opening of Bank Account (UOB)"),
    (re.compile(r"OPENING\s+OF\s+BANK\s+ACCOUNT.*?DBS", re.I), "Opening of Bank Account (DBS)"),
    (re.compile(r"OPENING\s+OF\s+BANK\s+ACCOUNT.*?(?:SILICON\s+VALLEY|SVB)", re.I), "Opening of Bank Account (SVB)"),
    (re.compile(r"OPENING\s+OF\s+BANK\s+ACCOUNT.*?OCBC", re.I), "Opening of Bank Account (OCBC)"),
    (re.compile(r"OPENING\s+OF\s+BANK\s+ACCOUNT", re.I), "Opening of Bank Account"),
    (re.compile(r"CONVENING\s+AN?\s+E(?:XTRA)?\.?\s*O(?:RDINARY)?\.?\s*G(?:ENERAL)?\.?\s*M(?:EETING)?", re.I), "Convening an EGM"),
    (re.compile(r"INCORPORATION", re.I), "Incorporation"),
    (re.compile(r"REGISTERED\s+OFFICE", re.I), "Registered Office"),
    (re.compile(r"CHANGE\s+OF\s+SECRETARY", re.I), "Change of Secretary"),
    (re.compile(r"CHANGE\s+OF\s+DIRECTOR", re.I), "Change of Director"),
    (re.compile(r"CHANGE\s+OF\s+R\.?O\.?\s+ADDRESS", re.I), "Change of RO Address"),
    (re.compile(r"CHANGE\s+OF\s+BANK\s+SIGNATOR", re.I), "Change of Bank Signatories"),
    (re.compile(r"AUTHORITY\s+TO\s+ISSUE\s+SHARES", re.I), "Authority to Issue Shares"),
    (re.compile(r"RESIGNATION\s+OF\s+SECRETARY", re.I), "Resignation of Secretary"),
    (re.compile(r"RESIGNATION\s+OF\s+DIRECTOR", re.I), "Resignation of Director"),
    (re.compile(r"APPROVAL\s+(?:AND\s+ADOPTION\s+)?OF\s+.*?FINANCIAL\s+STATEMENTS", re.I), "Approval of Financial Statements"),
    (re.compile(r"LETTER\s+OF\s+AUTHORITY", re.I), "Letter of Authority"),
    (re.compile(r"AGM|ANNUAL\s+GENERAL\s+MEETING", re.I), "AGM"),
]


def _extract_company_name(text: str) -> str | None:
    """Extract company name from the first few lines of page text."""
    # Flatten first ~300 chars to handle names split across lines by OCR
    header = " ".join(text[:300].split())
    m = re.search(
        r"([A-Z][\w\s.,&'()\-]+?(?:PTE[.,]?\s*LTD[.,]?|PRIVATE\s+LIMITED))",
        header,
        re.IGNORECASE,
    )
    if m:
        name = m.group(1).strip().rstrip(",.")
        # Clean OCR artifacts
        name = name.replace("Æ", "'").replace("ô", "'").replace("ö", "'")
        return name
    return None


def _is_subject_line(line: str) -> bool:
    """Check if a line looks like a resolution subject (action-like, mostly caps)."""
    if len(line) < 8:
        return False
    if _BOILERPLATE_RE.match(line):
        return False
    # Exclude address lines and labels
    if re.search(
        r"Street|Singapore\s+\d|Office\s*:\s*\d|#\d{2}-\d{2}|"
        r"^Registered\s+Office\s*:|Schedule\s+referred\s+to|"
        r"pertaining\s+to\s+the\s+Opening",
        line, re.IGNORECASE,
    ):
        return False
    # Must contain an action keyword
    line_upper = line.upper()
    return any(kw in line_upper for kw in _ACTION_KEYWORDS)


def _extract_raw_subject(text: str) -> str | None:
    """Extract the raw subject line from a resolution page.

    Scans lines in the header area for action keywords.
    Handles both formats: subject before RESOLVED and subject after RESOLVED.
    """
    for line in text[:1000].split("\n"):
        line = line.strip()
        if not line:
            continue
        if _is_subject_line(line):
            # Strip leading numbering like "1." or "1,"
            cleaned = re.sub(r"^\d+[.,]\s*", "", line)
            return cleaned if cleaned else line
    return None


def _normalize_subject(raw: str) -> str:
    """Normalize a raw OCR subject to a standard document type name."""
    for pattern, normalized in _SUBJECT_NORMALIZATIONS:
        if pattern.search(raw):
            return normalized
    # Fallback: title case
    return raw.strip().title()


def _subjects_match(subj1: str, subj2: str) -> bool:
    """Check if two raw subjects are the same resolution (multi-page continuation)."""
    n1 = _normalize_subject(subj1)
    n2 = _normalize_subject(subj2)
    return n1 == n2


def _resolve_month(raw: str) -> str | None:
    """Resolve a month string, handling OCR errors."""
    upper = raw.upper()
    # Try exact match first (full name or abbreviation)
    m = _MONTH_MAP.get(upper) or _MONTH_MAP.get(upper[:3])
    if m:
        return m
    # Try fuzzy OCR match on first 3 chars
    return _MONTH_FUZZY.get(upper[:3])


def _fix_ocr_day(raw: str) -> str | None:
    """Try to fix OCR-mangled day digits (e.g. '73' → '23').

    Common OCR digit confusions: 2↔7, 1↔4, 0↔8, 5↔6.
    Returns a valid day string (01-31) or None if unfixable.
    """
    try:
        val = int(raw)
    except (ValueError, TypeError):
        return None
    if 1 <= val <= 31:
        return raw.zfill(2)
    # Try swapping first digit with common OCR alternatives
    _OCR_DIGIT_ALTS = {"7": ["2", "1"], "4": ["1"], "8": ["0", "6"],
                       "6": ["5", "8"], "9": ["4"], "0": ["8"]}
    s = str(val)
    if len(s) == 2 and s[0] in _OCR_DIGIT_ALTS:
        for alt in _OCR_DIGIT_ALTS[s[0]]:
            candidate = int(alt + s[1])
            if 1 <= candidate <= 31:
                return str(candidate).zfill(2)
    return None


def _fix_ocr_year(raw: str) -> str | None:
    """Fix OCR-mangled year strings like '202!' or '202|' → '2021'."""
    # Replace common OCR substitutions for digits
    fixed = raw.replace("!", "1").replace("|", "1").replace("l", "1").replace("O", "0").replace("o", "0")
    if fixed.isdigit() and len(fixed) == 4:
        return fixed
    return None


def _extract_date_from_text(text: str) -> str | None:
    """Extract a date from page text, handling various formats and OCR errors."""

    # Pattern 1: "Date[d]: DD MMM YYYY" (with OCR separator noise)
    m = re.search(
        r"[Dd]ate[d]?\s*[:;=\s]*[-–]?\s*(\d{1,2})\s*[-/:;\s]*"
        r"([A-Za-z]{3,9})\s*[-/,\s]*(\S{4})",
        text,
    )
    if m:
        day = _fix_ocr_day(m.group(1))
        month = _resolve_month(m.group(2))
        year = _fix_ocr_year(m.group(3))
        if day and month and year:
            return f"{year}-{month}-{day}"

    # Pattern 2: "Dated this DDth day of MONTH YYYY"
    m = re.search(
        r"[Dd]ated\s+this\s+(\d{1,2})\s*(?:st|nd|rd|th|\"|\u201d|\u2019|.)?"
        r"\s*day\s+of\s+([A-Za-z]{3,9})\s*[-/,\s]*(\S{4})",
        text,
    )
    if m:
        day = _fix_ocr_day(m.group(1))
        month = _resolve_month(m.group(2))
        year = _fix_ocr_year(m.group(3))
        if day and month and year:
            return f"{year}-{month}-{day}"

    # Pattern 3: "DD MMM YYYY" standalone (no Date prefix)
    m = re.search(
        r"(?<!\d)(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})(?!\d)",
        text,
    )
    if m:
        day = _fix_ocr_day(m.group(1))
        month = _resolve_month(m.group(2))
        year = m.group(3)
        if day and month:
            return f"{year}-{month}-{day}"

    # Pattern 4: M/DD/YYYY (US-style from bank transfers)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        month = m.group(1).zfill(2)
        day = m.group(2).zfill(2)
        year = m.group(3)
        if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
            return f"{year}-{month}-{day}"

    return None


def _find_date_in_range(page_texts: list[str], start: int, end: int) -> str | None:
    """Search pages (0-indexed) from last to first for a resolution date."""
    for i in range(end, start - 1, -1):
        date = _extract_date_from_text(page_texts[i])
        if date:
            return date
    return None


def _detect_boundaries_heuristic(
    page_texts: list[str], doc_prefix: str = "DRIW",
) -> list[DocumentSegment]:
    """Detect document boundaries using regex patterns on resolution headers.

    More reliable than AI for structured corporate secretary documents.
    """
    page_count = len(page_texts)
    # Classify each page: (is_new_doc, raw_subject, company_name)
    boundaries: list[tuple[int, str, str | None]] = []  # (page_idx, subject, company)
    prev_norm_subject: str | None = None
    prev_raw_subject: str | None = None

    for i, text in enumerate(page_texts):
        header = text[:1000]

        # Skip known continuation page patterns (schedules, applications, etc.)
        first_line = header.split("\n", 1)[0].strip()
        if _CONTINUATION_START_RE.match(first_line):
            continue

        # Skip pages with "Page N" (N > 1) in header — continuation pages
        pn = _PAGE_NUM_RE.search(header[:300])
        if pn and int(pn.group(1)) > 1:
            continue

        has_resolution = bool(_RESOLUTION_RE.search(header))
        raw_subject = _extract_raw_subject(header) if has_resolution else None

        if has_resolution and raw_subject:
            norm = _normalize_subject(raw_subject)
            # New boundary if subject type OR raw wording changed (e.g. two
            # "Allotment of Shares" with different share counts)
            if norm != prev_norm_subject or raw_subject != prev_raw_subject:
                company = _extract_company_name(header)
                boundaries.append((i, raw_subject, company))
                prev_norm_subject = norm
                prev_raw_subject = raw_subject
            # Same subject as previous → multi-page resolution (continuation)
            continue

        # Check for non-resolution document starts (e.g. Letter of Authority)
        # Only check first few lines — body text deeper in the page must NOT
        # trigger a false boundary.
        if not has_resolution:
            non_res_subject = _extract_raw_subject(text[:300])
            if non_res_subject:
                company = _extract_company_name(text)
                prev_company = boundaries[-1][2] if boundaries else None
                if company and prev_company and not _companies_match(company, prev_company):
                    # Use the main document's company (previous boundary),
                    # not the foreign company on this page (e.g. Letter of
                    # Authority FROM another company still belongs to DDI).
                    boundaries.append((i, non_res_subject, prev_company))
                    prev_norm_subject = _normalize_subject(non_res_subject)

    if not boundaries:
        return []

    # Build segments from boundaries
    segments: list[DocumentSegment] = []
    for idx, (page_idx, raw_subject, company) in enumerate(boundaries):
        # End page is one before the next boundary, or last page
        if idx + 1 < len(boundaries):
            end_page = boundaries[idx + 1][0]  # 0-indexed of next boundary
        else:
            end_page = page_count  # last boundary extends to end

        # Convert to 1-based
        start_1 = page_idx + 1
        end_1 = end_page if idx + 1 < len(boundaries) else page_count

        # Find date within this segment's pages
        date = _find_date_in_range(page_texts, page_idx, end_1 - 1)

        norm_subject = _normalize_subject(raw_subject)
        doc_type = f"{doc_prefix}-{norm_subject}" if doc_prefix else norm_subject

        segments.append(DocumentSegment(
            page_start=start_1,
            page_end=end_1,
            company_name=company,
            document_type=_normalize_doc_type(doc_type),
            document_date=date,
        ))

    return segments


def _companies_match(name1: str, name2: str) -> bool:
    """Check if two company names refer to the same entity (fuzzy match)."""
    def norm(n: str) -> str:
        n = re.sub(r"[^a-z\s]", "", n.lower())
        n = re.sub(r"\b(pte|ltd|limited|private|investment)\b", "", n)
        return re.sub(r"\s+", " ", n).strip()
    return norm(name1) == norm(name2)


def _extract_doc_prefix_from_filename(filename: str) -> str:
    """Extract document prefix (DRIW, ACRA, etc.) from the PDF filename."""
    upper = filename.upper()
    for prefix in ("DRIW", "ACRA", "MRIW"):
        if prefix in upper:
            return prefix
    return "DRIW"  # default for corporate resolutions


# ---------------------------------------------------------------------------
# AI fallback boundary detection (for non-standard documents)
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
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        return m.group()

    # Response may be truncated (no closing ']'). Try to repair.
    m = re.search(r"\[", raw)
    if not m:
        return None

    text = raw[m.start():]
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
    """Format per-page texts for the AI prompt."""
    if len(page_texts) > 25:
        max_chars_per_page = 300
    elif len(page_texts) > 15:
        max_chars_per_page = 400

    parts = []
    for i, text in enumerate(page_texts):
        if len(text) <= max_chars_per_page:
            content = text
        else:
            tail = min(200, max_chars_per_page // 2)
            head = max_chars_per_page - tail
            content = text[:head] + "\n...\n" + text[-tail:]
        parts.append(f"--- PAGE {i + 1} ---\n{content}")
    return "\n\n".join(parts)


def _detect_boundaries_ai(page_texts: list[str]) -> list[DocumentSegment]:
    """AI fallback: send page texts to Ollama for boundary detection."""
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
        seg.page_start = max(1, min(seg.page_start, page_count))
        seg.page_end = max(1, min(seg.page_end, page_count))
        segments.append(seg)

    segments.sort(key=lambda s: s.page_start)

    if segments and segments[-1].page_end < page_count:
        segments[-1].page_end = page_count

    segments = [s for s in segments if s.page_start <= page_count]
    return segments


def _normalize_doc_type(doc_type: str | None) -> str | None:
    """Normalize document type to Title Case, preserving known prefixes/codes."""
    if not doc_type:
        return None

    # Words that should stay lowercase (unless first word)
    _LOWERCASE_WORDS = {"of", "the", "in", "for", "and", "or", "to", "a", "an"}
    # Acronyms/codes that should stay uppercase
    _UPPERCASE_WORDS = {"AGM", "EGM", "ACRA", "DRIW", "FY", "BODM", "DPO",
                        "UOB", "DBS", "SVB", "OCBC", "RO"}

    # Split on first dash to preserve prefix like "DRIW-"
    if "-" in doc_type:
        prefix, rest = doc_type.split("-", 1)
        prefix = prefix.upper()
        parts = rest.split()
        titled = []
        for i, word in enumerate(parts):
            # Strip parentheses for checking, preserve them in output
            core = word.strip("()")
            upper = core.upper()
            if re.match(r"^FY\d", core, re.IGNORECASE):
                titled.append(word.upper())
            elif upper in _UPPERCASE_WORDS:
                # Reconstruct with original parentheses but uppercase core
                titled.append(word.replace(core, upper))
            elif word.lower() in _LOWERCASE_WORDS and i > 0:
                titled.append(word.lower())
            elif word.isupper() and len(word) > 1:
                titled.append(word.title())
            else:
                titled.append(word)
        return f"{prefix}-{' '.join(titled)}"

    return doc_type


def _normalize_date(date_str: str | None) -> str | None:
    """Normalize a date string to YYYY-MM-DD. Returns None if unrecognizable."""
    if not date_str:
        return None

    # Already in YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str

    # Try DD/MM/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", date_str)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

    return None


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

def _display_segments(segments: list[DocumentSegment], filename: str = "") -> None:
    """Print the segment table with aligned columns and date warnings."""
    if filename:
        print(f"  File: {filename}")
        print()
    # Column header
    print(
        f"  {'#':>3}  {'Pages':<10}  {'Date':<12}  {'Abbr':<6}  {'Document Type'}"
    )
    print(f"  {'---':>3}  {'-----':<10}  {'----':<12}  {'----':<6}  {'-------------'}")

    for i, seg in enumerate(segments):
        abbr = seg.abbreviation or "???"
        date_str = seg.document_date or "no-date"
        doc_type = seg.document_type or "unknown"
        pages = f"{seg.page_start}-{seg.page_end}"

        # Highlight date issues
        if not seg.document_date:
            date_display = f"{'** ' + date_str + ' **':<12}"
        else:
            date_display = f"{date_str:<12}"

        print(
            f"  {i + 1:>3}. {pages:<10}  {date_display}  {abbr:<6}  {doc_type}"
        )

    # Show warning if any segments have no date
    no_date_segs = [i + 1 for i, s in enumerate(segments) if not s.document_date]
    if no_date_segs:
        nums = ", ".join(str(n) for n in no_date_segs)
        print(f"\n  ⚠ Segment(s) {nums} missing date — use 'e' to set manually")


def _interactive_review(
    segments: list[DocumentSegment], total_pages: int,
    filename: str = "",
) -> list[DocumentSegment] | None:
    """Show detected segments and let the user confirm, edit, or cancel.

    Returns updated segments list, or None if cancelled.
    """
    print("\n" + "=" * 70)
    print("  Detected document segments (review before splitting)")
    print("=" * 70)
    _display_segments(segments, filename)
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
            print()
            _display_segments(segments, filename)

        elif choice == "d":
            segments = _delete_segment(segments, total_pages)
            print()
            _display_segments(segments, filename)

        else:
            print("  Invalid choice. Press Enter to accept, 'e' to edit, 'd' to delete, 'c' to cancel.")


def _edit_segment(
    segments: list[DocumentSegment], total_pages: int,
) -> list[DocumentSegment]:
    """Edit one segment's fields interactively with input validation."""
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

    # page_start — must be a valid integer
    while True:
        val = input(f"    page_start [{seg.page_start}]: ").strip()
        if not val:
            break
        try:
            page = int(val)
            if 1 <= page <= total_pages:
                seg.page_start = page
                break
            print(f"    ⚠ Must be between 1 and {total_pages}. Try again.")
        except ValueError:
            print("    ⚠ Must be a number. Try again.")

    # page_end — must be a valid integer >= page_start
    while True:
        val = input(f"    page_end [{seg.page_end}]: ").strip()
        if not val:
            break
        try:
            page = int(val)
            if seg.page_start <= page <= total_pages:
                seg.page_end = page
                break
            print(f"    ⚠ Must be between {seg.page_start} and {total_pages}. Try again.")
        except ValueError:
            print("    ⚠ Must be a number. Try again.")

    val = input(f"    document_type [{seg.document_type}]: ").strip()
    if val:
        seg.document_type = val

    # document_date — must normalize to YYYY-MM-DD or "no-date"
    while True:
        val = input(f"    document_date [{seg.document_date or 'no-date'}]: ").strip()
        if not val:
            break
        normalized = _normalize_date(val)
        if normalized:
            seg.document_date = normalized
            break
        print("    ⚠ Invalid date format. Use DD/MM/YYYY or YYYY-MM-DD. Try again.")

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

    # Step 2: Heuristic boundary detection (primary) with AI fallback
    doc_prefix = _extract_doc_prefix_from_filename(pdf_path.name)
    logger.info("[Split 2] Detecting document boundaries (heuristic)...")
    segments = _detect_boundaries_heuristic(page_texts, doc_prefix)

    if len(segments) < 2:
        logger.info("[Split 2] Heuristic found %d segment(s) — trying AI fallback...",
                     len(segments))
        segments = _detect_boundaries_ai(page_texts)

    if not segments:
        logger.error("Could not identify any document segments.")
        return []

    method = "heuristic" if len(segments) >= 2 else "AI"
    logger.info("[Split 2] Detected %d document(s) via %s:", len(segments), method)
    for i, seg in enumerate(segments):
        logger.info(
            "  %d. Pages %d-%d | %s | %s | %s",
            i + 1, seg.page_start, seg.page_end,
            seg.document_date or "no-date",
            seg.abbreviation or "???",
            seg.document_type or "unknown",
        )

    # Step 2b: Interactive review — let user confirm or adjust
    segments = _interactive_review(segments, len(page_texts), filename=pdf_path.name)

    if not segments:
        logger.info("Split cancelled by user.")
        return []

    # Step 3: Split PDF
    logger.info("[Split 3] Splitting PDF into %d files...", len(segments))
    created = _split_pdf_by_segments(pdf_path, segments, output_folder)
    logger.info("[Split 3] Done — %d file(s) created.", len(created))

    return created
