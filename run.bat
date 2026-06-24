@echo off
REM ============================================================
REM NetSpeed Meter - Setup and Run Script (Windows)
REM ============================================================
REM This script creates a virtual environment (first run only),
REM installs dependencies, and launches the application.
REM ============================================================

cd /d "%~dp0"

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

echo Activating virtual environment...
call venv\Scripts\activate.bat

echo Installing dependencies...
pip install -r requirements.txt --quiet

echo Starting NetSpeed Meter...
pythonw speedmeter.py

REM If pythonw is not found, fall back to python (shows console window)
if errorlevel 1 (
    python speedmeter.py
)
