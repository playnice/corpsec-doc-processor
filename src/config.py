"""
Configuration module for CorpSec Document Processor.
Loads settings from .env and company mappings from Entity List CSV.
"""

import csv
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# --- Folder Paths (under data/ relative to project root) ---
_PROJECT_ROOT = Path(__file__).resolve().parent.parent  # repo root (parent of src/)
_DATA_DIR = _PROJECT_ROOT / "data"

load_dotenv(_PROJECT_ROOT / ".env")

WATCH_FOLDER = _DATA_DIR / "Inbox"
SPLIT_FOLDER = _DATA_DIR / "Split"
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

# --- Debug ---
DEBUG_DIR = _PROJECT_ROOT / "debug"
TEAMWORK_SCREENSHOTS_DIR = DEBUG_DIR / "Screenshots"

# --- Tesseract ---
TESSERACT_CMD = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# --- Entity suffixes to ignore when matching company names ---
_ENTITY_SUFFIXES = {
    "pte", "ltd", "pte.", "ltd.", "sdn", "bhd", "sdn.", "bhd.",
    "inc", "inc.", "corp", "corp.", "co", "co.",
    "limited", "private", "company", "incorporated",
    "llc", "llp", "lp", "plc", "pty", "pty.",
}

# ---------------------------------------------------------------------------
# Entity List CSV loader
# ---------------------------------------------------------------------------

ENTITY_LIST_PATH = _DATA_DIR / "Entity List.csv"

# Loaded at module import time:
#   _full_to_abbr: {"zoo capital ii (singapore)": "ZCII", ...}  (core name → abbr)
#   _abbr_to_full: {"ZCII": "ZOO CAPITAL II (SINGAPORE) PTE. LTD.", ...}
#   _full_raw:     {"ZCII": "ZOO CAPITAL II (SINGAPORE) PTE. LTD."}  (original CSV text)
_full_to_abbr: dict[str, str] = {}
_abbr_to_full: dict[str, str] = {}


def _strip_entity_suffix(name: str) -> str:
    """Remove entity suffixes (PTE, LTD, etc.) and return the core name, lowercased."""
    words = name.strip().split()
    core = [w for w in words if w.lower().rstrip(".,") not in _ENTITY_SUFFIXES]
    return " ".join(core).lower().strip()


def _load_entity_list() -> None:
    """Load company name → abbreviation mappings from CSV."""
    global _full_to_abbr, _abbr_to_full

    if not ENTITY_LIST_PATH.exists():
        logger.error("Entity List CSV not found: %s", ENTITY_LIST_PATH)
        return

    count = 0
    with open(ENTITY_LIST_PATH, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)  # skip header row
        if not header:
            logger.error("Entity List CSV is empty: %s", ENTITY_LIST_PATH)
            return

        for row in reader:
            if len(row) < 2 or not row[0].strip() or not row[1].strip():
                continue
            full_name = row[0].strip()
            abbr = row[1].strip().upper()
            core = _strip_entity_suffix(full_name)
            if core:
                _full_to_abbr[core] = abbr
            _abbr_to_full[abbr] = full_name
            count += 1

    logger.info("Loaded %d company mappings from %s", count, ENTITY_LIST_PATH.name)


# Load on import
_load_entity_list()


def get_company_short_name(full_name: str) -> str:
    """
    Look up the abbreviation for a company name.

    Matching is case-insensitive and ignores entity suffixes
    (PTE, LTD, SDN BHD, etc.). Returns 'UNKNOWN' if no match found.
    """
    core = _strip_entity_suffix(full_name)
    if not core:
        return "UNKNOWN"

    # Direct core match
    if core in _full_to_abbr:
        return _full_to_abbr[core]

    # Fuzzy: check if any known core is contained in input or vice versa
    for known_core, abbr in _full_to_abbr.items():
        if known_core in core or core in known_core:
            return abbr

    logger.error("Company not found in Entity List: '%s'", full_name)
    return "UNKNOWN"


def get_company_full_name(short_name: str) -> str | None:
    """Reverse lookup: given an abbreviation (e.g. 'BXI'), return the
    full company name from the Entity List CSV.

    Returns None if no mapping found.
    """
    upper = short_name.strip().upper()
    return _abbr_to_full.get(upper)


def get_all_company_cores() -> dict[str, str]:
    """Return all known company core names (lowercase, no suffixes) → abbreviation.

    Used by ai_analyzer for validating AI-extracted company names against
    known companies.
    """
    return dict(_full_to_abbr)
