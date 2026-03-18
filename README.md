# CorpSec Document Processor

Automated pipeline for processing scanned corporate secretary documents:
**Scan → OCR → AI Analysis → Smart Rename → Upload to Teamwork.sg**

## Architecture

```
Scanned PDF (dropped into data/Inbox/)
     │
     ▼
OCR (PyMuPDF direct / Tesseract fallback)
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
Match company to Entity List CSV → Abbreviation
     │
     ▼
Generate Filename: YYYYMMDD Abbr-Document Type.pdf
     │
     ▼
Rename & Move to Renamed folder
     │
     ▼
Upload to Teamwork.sg (browser automation via Playwright)
```

## Processing Modes

| Mode | Description | Input Folder | Output |
|------|-------------|-------------|--------|
| **1. Rename Only** | OCR + AI → rename | `data/Inbox/` | `data/Renamed/` |
| **2. Rename & Upload** | OCR + AI → rename → upload to Teamwork | `data/Inbox/` | `data/Uploaded/` |
| **3. Upload Only** | Upload pre-named files to Teamwork | `data/Renamed/` | `data/Uploaded/` |
| **4. Split Combined PDF** | Split multi-document PDFs into individual files | `data/Split/` | `data/Renamed/` |

## Project Structure

```
corpsec-doc-processor/
├── run.py                    ← Launcher (run from project root)
├── src/                      ← All Python source code
│   ├── __init__.py
│   ├── main.py               ← Entry point, mode selection & pipeline logic
│   ├── config.py             ← Configuration (paths, credentials, Entity List CSV)
│   ├── ai_analyzer.py        ← Ollama/Llama3.2 metadata extraction
│   ├── ocr_engine.py         ← OCR text extraction (PyMuPDF + Tesseract)
│   ├── renamer.py            ← Filename generation & rename logic
│   ├── pdf_splitter.py       ← Combined PDF splitting (heuristic + AI fallback)
│   ├── watcher.py            ← Folder watcher (watchdog)
│   └── teamwork_uploader.py  ← Playwright browser automation for teamwork.sg
├── tools/                    ← Development & discovery utilities
│   └── discover_teamwork.py  ← Interactive selector discovery for teamwork.sg
├── docs/                     ← Reference screenshots & documentation
├── data/                     ← Runtime data (gitignored)
│   ├── Entity List.csv       ← Company name ↔ abbreviation mappings
│   ├── Inbox/                ← Drop scanned PDFs here (mode 1 & 2)
│   ├── Renamed/              ← Renamed files ready for upload
│   ├── Split/                ← Drop combined PDFs here (mode 4)
│   ├── Uploaded/             ← Files successfully uploaded to Teamwork
│   └── Errors/               ← Problem files for manual review
├── debug/                    ← Debug artifacts
│   └── Screenshots/          ← Browser automation debug screenshots & HTML
├── setup.bat                 ← One-click setup script (Windows)
├── run.bat                   ← Quick launcher (activates venv + runs)
├── .env / .env.example       ← Environment configuration
├── .gitignore
├── README.md
└── requirements.txt
```

## Prerequisites

1. **Python 3.11+** — [Download](https://www.python.org/downloads/) (check "Add Python to PATH" during install)
2. **Tesseract OCR** — [Download for Windows](https://github.com/UB-Mannheim/tesseract/wiki)
3. **Ollama** — [Download](https://ollama.ai) then pull the model:
   ```bash
   ollama pull llama3.2
   ```
4. **Teamwork.sg account** (optional — for upload modes 2 & 3)

## Installation

### Quick Setup (Recommended)

Double-click `setup.bat` — it will:
1. Verify Python, Tesseract, and Ollama are installed
2. Create a virtual environment and install all dependencies
3. Install Playwright Chromium browser
4. Create data folders and `.env` configuration
5. Pull the Ollama model

After setup, edit `.env` with your Teamwork.sg credentials.

### Manual Setup

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

3. Add company mappings in `data/Entity List.csv`:
   ```csv
   Company Name,Abbreviation
   Zoo Capital II (Singapore) Pte Ltd,ZCII
   Dairy Dynasty Investment Pte Ltd,DDI
   ```
   Company matching ignores suffixes (Pte Ltd, Pty, Sdn Bhd, etc.) and is case-insensitive.

## Usage

### Quick Start

Double-click `run.bat` or:

```bash
venv\Scripts\activate
python run.py
```

On startup you'll be prompted to select a processing mode (1–4). Drop PDFs into the appropriate folder and they'll be processed automatically.

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
| Scanned PDF containing "Zoo Capital II Pte Ltd", "Authority to Issue Shares", dated "19 MAY 2022" | `20220519 ZCII-Authority to Issue Shares.pdf` |
| PDF with "Dairy Dynasty Investment Pte Ltd", "Board Resolution", dated "3 Jan 2024" | `20240103 DDI-Board Resolution.pdf` |

**Format:** `YYYYMMDD Abbreviation-Document Type.pdf`

Abbreviations are looked up from `data/Entity List.csv`. If a company is not found, the file is moved to `data/Errors/`.

## PDF Split Feature (Mode 4)

Splits combined multi-document PDFs (e.g. a stack of Directors' Resolutions scanned as one file) into individual named documents.

### How It Works

1. **Per-page OCR** — Extracts text from every page using Tesseract
2. **Heuristic boundary detection** — Uses regex patterns to identify "Directors' Resolution in Writing" headers, extract subjects, and detect date patterns
3. **AI fallback** — If heuristic finds fewer than 2 segments, falls back to Ollama AI analysis
4. **Interactive review** — Shows detected segments in a table for user confirmation:

```
======================================================================
  Detected document segments (review before splitting)
======================================================================
    #  Pages       Date          Abbr    Document Type
  ---  -----       ----          ----    -------------
    1. 1-3         2021-12-27    DDI     DRIW-Appointment of Alternate Director
    2. 4-4         2021-08-23    DDI     DRIW-Appointment of Auditors
    3. 5-8         2021-07-26    DDI     DRIW-Allotment of Shares
    4. 9-13        ** no-date **  DDI     DRIW-Declaration of Interests
======================================================================
  ⚠ Segment(s) 4 missing date — use 'e' to set manually
```

**Review options:**
- **Enter** — Accept and split into individual PDFs
- **e** — Edit a segment (pages, date, type, company)
- **d** — Delete a segment (merge into previous)
- **c** — Cancel

### OCR Error Correction

The date extractor handles common OCR artifacts:
- Mangled digits: `73` → `23` (7↔2 confusion), `202!` → `2021` (!↔1)
- Fuzzy months: `JNN` → `JUN`, `AUC` → `AUG`
- Format variants: `Dated this 01st day of June 2021`, `Date: = 07 JUN 2021`

## Teamwork.sg Upload Workflow

When mode 2 or 3 is selected, the browser automation performs:

1. Login with company ID, username, password
2. Navigate to Records → Companies
3. Search for the company (entity suffixes stripped for matching)
4. Click View on the matching company row
5. Go to the Files tab
6. Click Upload
7. Choose the PDF file
8. Fill in Description (from document type, stripping prefixes like "ACRA-")
9. Set Resolution Date (from extracted/edited date)
10. Select Categories via fuzzy matching (e.g. ACRA files → "ACRA Lodgements", DRIW files → "Directors' Written Resolution")
11. Click Save

### Category Auto-Selection

| Filename contains | Category assigned |
|-------------------|-------------------|
| `ACRA` | ACRA Lodgements |
| `DRIW` | Directors' Written Resolution / Minutes of BODM |

Additional categories are matched via fuzzy text comparison against the document type.

## Supported Document Types

The system recognizes common corporate secretary documents:
- Allotment of Shares / Return of Allotment
- Appointment/Resignation of Director/Secretary/Auditors
- Declaration of Interests
- Opening of Bank Account (UOB, DBS, SVB, OCBC)
- Authority to Issue Shares
- Board Resolution / Directors Resolution in Writing
- Certificate of Incorporation / First Board Resolutions
- Annual General Meeting / AGM Financial Statements
- Convening an EGM
- Letter of Authority
- Registered Office changes
- And more (the AI generalizes from context)

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "No text extracted" | Ensure Tesseract is installed and `TESSERACT_CMD` path is correct |
| "Ollama analysis failed" | Run `ollama serve` and ensure model is pulled: `ollama pull llama3.2` |
| Low confidence results | The scanned document may be poor quality — check `data/Errors/` |
| Company not found | Add the company to `data/Entity List.csv` |
| Teamwork upload fails | Verify credentials in `.env`. Set `TEAMWORK_HEADLESS=false` to watch the browser. Check `debug/Screenshots/` for debug captures. |
| PDF split over-segments | Use `e` to edit or `d` to delete/merge segments during review |
| Date shows "no-date" | OCR may be too garbled — use `e` to manually set the date during review |
