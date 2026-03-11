# CorpSec Document Processor

Automated pipeline for processing scanned corporate secretary documents:
**Scan → OCR → AI Analysis → Smart Rename → Upload to Teamwork**

## Architecture

```
Scanned PDF (dropped into Inbox folder)
     │
     ▼
OCR (PyMuPDF direct / OCRmyPDF+Tesseract fallback)
     │
     ▼
Extract Text
     │
     ▼
Ollama Llama3 (Local AI)
     ├─ Detect Document Type
     ├─ Extract Company Name
     ├─ Extract Date
     └─ Confidence Score
     │
     ▼
Generate Filename: YYYYMMDD ShortName-Document Type.pdf
     │
     ▼
Rename & Move to Processed folder
     │
     ▼
Upload to Teamwork (project/entity, category, resolution date)
```

## Prerequisites

1. **Python 3.11+**
2. **Tesseract OCR** — [Download for Windows](https://github.com/UB-Mannheim/tesseract/wiki)
3. **Ollama** — [Download](https://ollama.ai) then pull the model:
   ```bash
   ollama pull llama3
   ```
4. **Teamwork account** (optional — for upload step)

## Installation

```bash
# Clone / copy this project
cd corpsec-doc-processor

# Create virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

# Install dependencies
pip install -r requirements.txt
```

## Configuration

1. Copy `.env.example` to `.env`:
   ```bash
   copy .env.example .env
   ```

2. Edit `.env` with your settings:
   ```ini
   WATCH_FOLDER=C:\CorpSec\Inbox
   PROCESSED_FOLDER=C:\CorpSec\Processed
   ERROR_FOLDER=C:\CorpSec\Errors
   OLLAMA_BASE_URL=http://localhost:11434
   OLLAMA_MODEL=llama3
   TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
   TEAMWORK_BASE_URL=https://yourcompany.teamwork.com
   TEAMWORK_API_KEY=your-api-key-here
   ```

3. Add company short-name mappings in `config.py`:
   ```python
   COMPANY_SHORT_NAMES = {
       "zoo capital fund ii pte ltd": "ZCFII",
       "acme holdings pte ltd": "ACME",
   }
   ```

## Usage

### Watch Mode (Continuous)
Drop PDFs into the inbox folder and they'll be processed automatically:
```bash
python main.py
```

### Single File Mode
Process one PDF and exit:
```bash
python main.py --once "C:\path\to\scanned.pdf"
```

## Naming Convention

| Input | Output |
|-------|--------|
| Scanned PDF containing "Zoo Capital Fund II Pte Ltd", "AUTHORITY TO ISSUE SHARES", dated "19 MAY 2022" | `20220519 ZCFII-Authority to Issue Shares.pdf` |
| PDF with "Acme Holdings Pte Ltd", "Board Resolution", dated "3 Jan 2024" | `20240103 ACME-Board Resolution.pdf` |

**Format:** `YYYYMMDD CompanyShortForm-Document Type.pdf`

## Folder Structure

```
C:\CorpSec\
├── Inbox\       ← Drop scanned PDFs here
├── Processed\   ← Renamed PDFs moved here after processing
└── Errors\      ← Files that couldn't be processed (manual review)
```

## Supported Document Types

The AI model recognizes common corporate secretary documents:
- Authority to Issue Shares
- Board Resolution / Directors Resolution in Writing
- Share Transfer Form / Return of Allotment
- Certificate of Incorporation
- Memorandum & Articles of Association / Constitution
- Annual Return / Annual General Meeting
- Notice of AGM / Minutes of Meeting
- Change of Director / Secretary
- Registered Office changes
- And more (the AI generalizes from context)

## Teamwork Integration

When configured, documents are automatically uploaded to Teamwork:
- **Project** is matched by company name
- **Category** is auto-mapped from document type (e.g., "Authority to Issue Shares" → "Share Capital")
- Files appear in the project's Files section

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "No text extracted" | Ensure Tesseract is installed and `TESSERACT_CMD` path is correct |
| "Ollama analysis failed" | Run `ollama serve` and ensure model is pulled: `ollama pull llama3` |
| Low confidence results | The scanned document may be poor quality — check the Errors folder |
| Teamwork upload fails | Verify `TEAMWORK_API_KEY` and `TEAMWORK_BASE_URL` in `.env` |
