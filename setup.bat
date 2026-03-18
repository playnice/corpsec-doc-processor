@echo off
setlocal enabledelayedexpansion

:: ============================================================================
::  CorpSec Document Processor — One-Click Setup
:: ============================================================================
echo.
echo  ==================================================
echo    CorpSec Document Processor — Setup
echo  ==================================================
echo.

:: -------------------------------------------------------------------
:: 1. Check Python
:: -------------------------------------------------------------------
echo  [1/7] Checking Python...

python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python is not installed or not in PATH.
    echo          Download from: https://www.python.org/downloads/
    echo          Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo  [OK] Python %PYVER% found.

:: Check version >= 3.11
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set PYMAJOR=%%a
    set PYMINOR=%%b
)
if %PYMAJOR% LSS 3 (
    echo  [ERROR] Python 3.11+ required, found %PYVER%.
    pause
    exit /b 1
)
if %PYMAJOR%==3 if %PYMINOR% LSS 11 (
    echo  [ERROR] Python 3.11+ required, found %PYVER%.
    pause
    exit /b 1
)

:: -------------------------------------------------------------------
:: 2. Check Tesseract OCR
:: -------------------------------------------------------------------
echo.
echo  [2/7] Checking Tesseract OCR...

set TESSERACT_FOUND=0
set TESSERACT_PATH=

:: Check common install locations
if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
    set TESSERACT_FOUND=1
    set TESSERACT_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe
)
if exist "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe" (
    set TESSERACT_FOUND=1
    set TESSERACT_PATH=C:\Program Files (x86)\Tesseract-OCR\tesseract.exe
)

:: Check PATH
if %TESSERACT_FOUND%==0 (
    tesseract --version >nul 2>&1
    if not errorlevel 1 (
        set TESSERACT_FOUND=1
        for /f "delims=" %%p in ('where tesseract 2^>nul') do set TESSERACT_PATH=%%p
    )
)

if %TESSERACT_FOUND%==1 (
    echo  [OK] Tesseract found: %TESSERACT_PATH%
) else (
    echo  [WARNING] Tesseract OCR not found.
    echo            Download from: https://github.com/UB-Mannheim/tesseract/wiki
    echo            Install it, then re-run this setup.
    echo.
    set /p CONTINUE="  Continue anyway? (y/n): "
    if /i not "!CONTINUE!"=="y" exit /b 1
)

:: -------------------------------------------------------------------
:: 3. Check Ollama
:: -------------------------------------------------------------------
echo.
echo  [3/7] Checking Ollama...

set OLLAMA_FOUND=0
ollama --version >nul 2>&1
if not errorlevel 1 set OLLAMA_FOUND=1

if %OLLAMA_FOUND%==1 (
    echo  [OK] Ollama found.
) else (
    echo  [WARNING] Ollama not found.
    echo            Download from: https://ollama.ai
    echo            Install it, then re-run this setup.
    echo.
    set /p CONTINUE="  Continue anyway? (y/n): "
    if /i not "!CONTINUE!"=="y" exit /b 1
)

:: -------------------------------------------------------------------
:: 4. Create virtual environment
:: -------------------------------------------------------------------
echo.
echo  [4/7] Setting up Python virtual environment...

if not exist "venv" (
    python -m venv venv
    if errorlevel 1 (
        echo  [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo  [OK] Virtual environment created.
) else (
    echo  [OK] Virtual environment already exists.
)

:: Activate venv
call venv\Scripts\activate.bat

:: -------------------------------------------------------------------
:: 5. Install Python dependencies
:: -------------------------------------------------------------------
echo.
echo  [5/7] Installing Python dependencies...

pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo  [ERROR] Failed to install Python dependencies.
    pause
    exit /b 1
)
echo  [OK] Python dependencies installed.

:: Install Playwright browser
echo.
echo  Installing Playwright Chromium browser...
playwright install chromium --quiet 2>nul
if errorlevel 1 (
    playwright install chromium
)
echo  [OK] Playwright Chromium installed.

:: -------------------------------------------------------------------
:: 6. Create data folders and config
:: -------------------------------------------------------------------
echo.
echo  [6/7] Creating folders and configuration...

if not exist "data\Inbox" mkdir "data\Inbox"
if not exist "data\Renamed" mkdir "data\Renamed"
if not exist "data\Uploaded" mkdir "data\Uploaded"
if not exist "data\Errors" mkdir "data\Errors"
if not exist "data\Split" mkdir "data\Split"
if not exist "debug\Screenshots" mkdir "debug\Screenshots"
echo  [OK] Data folders created.

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo  [OK] Created .env from template.
    echo.
    echo  -------------------------------------------------
    echo   IMPORTANT: Edit .env with your settings:
    echo   - Teamwork.sg credentials
    echo   - Tesseract path (if non-default location^)
    echo  -------------------------------------------------
) else (
    echo  [OK] .env already exists.
)

:: Update Tesseract path in .env if found
if %TESSERACT_FOUND%==1 (
    if not "%TESSERACT_PATH%"=="" (
        python -c "p=r'%TESSERACT_PATH%'; lines=open('.env').readlines(); f=open('.env','w'); [f.write(f'TESSERACT_CMD={p}\n' if l.startswith('TESSERACT_CMD=') else l) for l in lines]; f.close()" 2>nul
    )
)

:: Check Entity List
if not exist "data\Entity List.csv" (
    echo.
    echo  [WARNING] data\Entity List.csv not found.
    echo            This file maps company names to abbreviations.
    echo            Create it with columns: Company Name,Abbreviation
    echo.
    :: Create a template
    echo Company Name,Abbreviation> "data\Entity List.csv"
    echo  [OK] Created template Entity List.csv
)

:: -------------------------------------------------------------------
:: 7. Pull Ollama model
:: -------------------------------------------------------------------
echo.
echo  [7/7] Pulling Ollama model...

if %OLLAMA_FOUND%==1 (
    echo  Pulling llama3.2 (this may take a few minutes on first run^)...
    ollama pull llama3.2
    if errorlevel 1 (
        echo  [WARNING] Could not pull model. Make sure Ollama is running.
        echo            You can pull it later with: ollama pull llama3.2
    ) else (
        echo  [OK] Model llama3.2 ready.
    )
) else (
    echo  [SKIP] Ollama not installed — pull the model later:
    echo         ollama pull llama3.2
)

:: -------------------------------------------------------------------
:: Done!
:: -------------------------------------------------------------------
echo.
echo  ==================================================
echo    Setup Complete!
echo  ==================================================
echo.
echo  To run the program:
echo    1. Activate venv:   venv\Scripts\activate
echo    2. Run:             python run.py
echo.
echo  Or simply double-click: run.bat
echo  ==================================================
echo.
pause
