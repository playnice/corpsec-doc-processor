"""
Configuration module for CorpSec Document Processor.
Loads settings from .env and defines company shortname mappings.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# --- Folder Paths (under data/ relative to project root) ---
_PROJECT_ROOT = Path(__file__).resolve().parent.parent  # repo root (parent of src/)
_DATA_DIR = _PROJECT_ROOT / "data"

load_dotenv(_PROJECT_ROOT / ".env")

WATCH_FOLDER = _DATA_DIR / "Inbox"
RENAMED_FOLDER = _DATA_DIR / "Renamed"
UPLOADED_FOLDER = _DATA_DIR / "Uploaded"
ERROR_FOLDER = _DATA_DIR / "Errors"

# --- Ollama ---
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

# --- Teamwork.sg (browser automation) ---
TEAMWORK_LOGIN_URL = os.getenv("TEAMWORK_LOGIN_URL", "https://login.teamwork.sg/")
TEAMWORK_COMPANY_ID = os.getenv("TEAMWORK_COMPANY_ID", "")
TEAMWORK_USERNAME = os.getenv("TEAMWORK_USERNAME", "")
TEAMWORK_PASSWORD = os.getenv("TEAMWORK_PASSWORD", "")
TEAMWORK_HEADLESS = os.getenv("TEAMWORK_HEADLESS", "true").lower() in ("true", "1", "yes")
TEAMWORK_SCREENSHOTS_DIR = _DATA_DIR / "Screenshots"

# --- Tesseract ---
TESSERACT_CMD = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# --- Company Name to Short Form Mappings ---
# Add your company name mappings here.
# The AI will attempt to match extracted company names to these entries.
# Keys should be lowercase for case-insensitive matching.
COMPANY_SHORT_NAMES: dict[str, str] = {
    "zoo capital ii (singapore) pte ltd": "ZCII",
    "zoo capital ii (singapore) pte. ltd.": "ZCII",
    "zoo capital fund ii pte ltd": "ZCFII",
    "zoo capital fund ii pte. ltd.": "ZCFII",
    "broad xiangshan investment pte ltd": "BXI",
    "broad xiangshan investment pte. ltd.": "BXI",
    # Add more mappings as needed:
    # "acme holdings pte ltd": "ACME",
    # "global ventures sdn bhd": "GVSB",
}


# Words to exclude when generating abbreviations from company names
_ENTITY_SUFFIXES = {
    "pte", "ltd", "pte.", "ltd.", "sdn", "bhd", "sdn.", "bhd.",
    "inc", "inc.", "corp", "corp.", "co", "co.",
    "limited", "private", "company", "incorporated",
    "llc", "llp", "lp", "plc",
}


def get_company_short_name(full_name: str) -> str:
    """
    Look up the short name for a company. Falls back to generating
    an abbreviation from capital letters if no mapping exists.
    """
    lookup = full_name.strip().lower()

    # Direct match
    if lookup in COMPANY_SHORT_NAMES:
        return COMPANY_SHORT_NAMES[lookup]

    # Fuzzy: check if any key is contained in the extracted name or vice versa
    for key, short in COMPANY_SHORT_NAMES.items():
        if key in lookup or lookup in key:
            return short

    # Fallback: generate abbreviation from meaningful words only
    # e.g. "Zoo Capital Fund II Pte Ltd" -> "ZCFII" (not "ZCFIPL")
    abbreviation = "".join(
        word[0].upper()
        for word in full_name.split()
        if word[0].isalpha() and word.lower().rstrip(".,") not in _ENTITY_SUFFIXES
    )
    return abbreviation if abbreviation else "UNKNOWN"


def get_company_full_name(short_name: str) -> str | None:
    """Reverse lookup: given a short name (e.g. 'BXI'), return the full company name.

    Returns None if no mapping found.
    """
    upper = short_name.strip().upper()
    _ROMAN = {"I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"}
    for full, short in COMPANY_SHORT_NAMES.items():
        if short.upper() == upper:
            words = []
            for w in full.split():
                if w.upper() in _ROMAN:
                    words.append(w.upper())
                else:
                    words.append(w.capitalize())
            return " ".join(words)
    return None
