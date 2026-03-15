# CorpSec Document Processor

Automated pipeline for processing scanned corporate secretary documents:
**Scan → OCR → AI Analysis → Smart Rename → Upload to Teamwork.sg**

## Architecture

```
Scanned PDF (dropped into data/Inbox/)
     │
     ▼
OCR (PyMuPDF direct / OCRmyPDF+Tesseract fallback)
     │
     ▼
Extract Text
     │
     ▼
Ollama Llama3.2 (Local AI)
     ├─ Detect Document Type
     ├─ Extract Company Name
     ├─ Extract Date
     ├─ Extract Document Content/Subject
     └─ Confidence Score
     │
     ▼
Generate Filename: YYYYMMDD ShortName-Document Type.pdf
     │
     ▼
Rename & Move to Renamed folder
     │
     ▼
Upload to Teamwork.sg (browser automation via Playwright)
```

## Project Structure

```
corpsec-doc-processor/
├── run.py                   ← Launcher (run from project root)
├── src/                     ← All Python source code
│   ├── __init__.py
│   ├── main.py              ← Entry point & pipeline logic
│   ├── config.py            ← Configuration (paths, credentials, mappings)
│   ├── ai_analyzer.py       ← Ollama/Llama3.2 metadata extraction
│   ├── ocr_engine.py        ← OCR text extraction (PyMuPDF + OCRmyPDF)
│   ├── renamer.py           ← Filename generation & rename logic
│   ├── watcher.py           ← Folder watcher (watchdog)
│   ├── teamwork_uploader.py ← Playwright browser automation for teamwork.sg
│   └── tesseract_words.txt  ← Custom OCR word list
├── tools/                   ← Development & discovery utilities
│   └── discover_teamwork.py ← Interactive selector discovery for teamwork.sg
├── docs/                    ← Reference screenshots & documentation
│   ├── Dashboard after login.png
│   └── Login page.png
├── data/                    ← Runtime data (gitignored)
│   ├── Inbox/               ← Drop scanned PDFs here
│   ├── Renamed/             ← Renamed files (mode 1 & 2)
│   ├── Uploaded/            ← Files uploaded to Teamwork (mode 2 & 3)
│   ├── Errors/              ← Problem files for manual review
│   └── Screenshots/         ← Debug screenshots from browser automation
├── .env / .env.example      ← Environment configuration
├── .gitignore
├── README.md
└── requirements.txt
```

## Prerequisites

1. **Python 3.11+**
2. **Tesseract OCR** — [Download for Windows](https://github.com/UB-Mannheim/tesseract/wiki)
3. **Ollama** — [Download](https://ollama.ai) then pull the model:
   ```bash
   ollama pull llama3.2
   ```
4. **Teamwork.sg account** (optional — for upload step)

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

# Install Playwright browsers (for Teamwork upload)
playwright install chromium
```

## Configuration

1. Copy `.env.example` to `.env`:
   ```bash
   copy .env.example .env
   ```

2. Edit `.env` with your settings:
   ```ini
   OLLAMA_BASE_URL=http://localhost:11434
   OLLAMA_MODEL=llama3.2
   TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
   TEAMWORK_LOGIN_URL=https://login.teamwork.sg/
   TEAMWORK_COMPANY_ID=your-company-id
   TEAMWORK_USERNAME=your-username
   TEAMWORK_PASSWORD=your-password
   TEAMWORK_HEADLESS=false
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
Drop PDFs into `data/Inbox/` and they'll be processed automatically:
```bash
python run.py
```

On startup you'll be prompted to select a processing mode:
1. **Rename Only** — OCR + AI → rename → `data/Renamed/`
2. **Rename & Upload** — OCR + AI → rename → upload to Teamwork → `data/Uploaded/`
3. **Upload Only** — Upload as-is to Teamwork → `data/Uploaded/`

### Single File Mode
Process one PDF and exit:
```bash
python run.py --once "C:\path\to\scanned.pdf"
```

### Discovery Tool
Interactive script to discover and verify teamwork.sg selectors:
```bash
python tools/discover_teamwork.py
python tools/discover_teamwork.py --step 8    # Skip to a specific step
```

## Naming Convention

| Input | Output |
|-------|--------|
| Scanned PDF containing "Zoo Capital Fund II Pte Ltd", "AUTHORITY TO ISSUE SHARES", dated "19 MAY 2022" | `20220519 ZCFII-Authority to Issue Shares.pdf` |
| PDF with "Acme Holdings Pte Ltd", "Board Resolution", dated "3 Jan 2024" | `20240103 ACME-Board Resolution.pdf` |

**Format:** `YYYYMMDD CompanyShortForm-Document Type.pdf`

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

## Teamwork.sg Upload Workflow

When mode 2 or 3 is selected, the browser automation performs:
1. Login with company ID, username, password
2. Navigate to Records → Companies
3. Search for the company (entity suffixes like "Pte Ltd" are stripped)
4. Click View on the matching company row
5. Go to the Files tab
6. Click Upload
7. Choose the PDF file
8. Fill in Description (from AI-extracted document content)
9. Set Resolution Date (from AI-extracted date)
10. Select Category via fuzzy matching to teamwork.sg categories
11. Click Save

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "No text extracted" | Ensure Tesseract is installed and `TESSERACT_CMD` path is correct |
| "Ollama analysis failed" | Run `ollama serve` and ensure model is pulled: `ollama pull llama3.2` |
| Low confidence results | The scanned document may be poor quality — check `data/Errors/` |
| Teamwork upload fails | Verify credentials in `.env`. Set `TEAMWORK_HEADLESS=false` to watch the browser. Check `data/Screenshots/` for debug captures. |
