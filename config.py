"""
Configuration module for CorpSec Document Processor.
Loads settings from .env and defines company shortname mappings.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- Folder Paths ---
WATCH_FOLDER = Path(os.getenv("WATCH_FOLDER", r"C:\CorpSec\Inbox"))
PROCESSED_FOLDER = Path(os.getenv("PROCESSED_FOLDER", r"C:\CorpSec\Processed"))
ERROR_FOLDER = Path(os.getenv("ERROR_FOLDER", r"C:\CorpSec\Errors"))

# --- Ollama ---
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
# Vision model for scanned PDFs — reads page images directly, bypassing Tesseract.
# Recommended: llama3.2-vision (best accuracy for document text)
# Alternatives: llama3.2-vision:90b (more accurate, very heavy), minicpm-v (lightweight), gemma3:12b
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "llama3.2-vision")

# --- Teamwork ---
TEAMWORK_BASE_URL = os.getenv("TEAMWORK_BASE_URL", "")
TEAMWORK_API_KEY = os.getenv("TEAMWORK_API_KEY", "")

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# --- Company Name to Short Form Mappings ---
# Add your company name mappings here.
# The AI will attempt to match extracted company names to these entries.
# Keys should be lowercase for case-insensitive matching.
COMPANY_SHORT_NAMES: dict[str, str] = {
    "zoo capital fund ii pte ltd": "ZCFII",
    "zoo capital fund ii pte. ltd.": "ZCFII",
    "zoo capital fund i pte ltd": "ZCFI",
    "zoo capital fund i pte. ltd.": "ZCFI",
    # Add more mappings as needed:
    # "acme holdings pte ltd": "ACME",
    # "global ventures sdn bhd": "GVSB",
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

    # Fallback: generate abbreviation from capital letters of original name
    # e.g. "Zoo Capital Fund II Pte Ltd" -> "ZCFIPL"
    abbreviation = "".join(
        word[0].upper()
        for word in full_name.split()
        if word[0].isalpha()
    )
    return abbreviation if abbreviation else "UNKNOWN"
