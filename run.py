"""
CorpSec Document Processor — Launcher
Run from project root: python run.py
"""
import sys
from pathlib import Path

# Add src/ to Python path so modules can import each other
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from main import main

if __name__ == "__main__":
    main()
