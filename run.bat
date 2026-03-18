@echo off
:: CorpSec Document Processor — Quick Launcher
cd /d "%~dp0"
call venv\Scripts\activate.bat
python run.py %*
