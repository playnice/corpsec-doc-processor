"""
AI Analyzer module.
Uses Ollama (Llama3) to extract structured metadata from document text:
  - Company name
  - Document type / title
  - Document date
"""

import json
import logging
from dataclasses import dataclass

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


def _parse_response(raw: str) -> DocumentMetadata:
    """Parse the JSON response from Ollama into DocumentMetadata."""
    # Strip markdown code fences if the model wraps output
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first and last lines (``` markers)
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON object within the response
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                logger.error("Could not parse Ollama response as JSON: %s", raw[:200])
                return DocumentMetadata(None, None, None, "low")
        else:
            logger.error("No JSON found in Ollama response: %s", raw[:200])
            return DocumentMetadata(None, None, None, "low")

    return DocumentMetadata(
        company_name=data.get("company_name"),
        document_type=data.get("document_type"),
        document_date=data.get("document_date"),
        confidence=data.get("confidence", "low"),
    )
