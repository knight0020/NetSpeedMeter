@echo off
REM ============================================================
REM NetSpeed Meter - Build standalone .exe with PyInstaller
REM ============================================================
REM Produces dist\NetSpeedMeter.exe - a single file that runs
REM on any Windows machine without needing Python installed.
REM ============================================================

cd /d "%~dp0"

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat

echo Installing dependencies + PyInstaller...
pip install -r requirements.txt --quiet
pip install pyinstaller --quiet

echo Building executable (this can take a minute)...
pyinstaller --noconfirm --onefile --windowed ^
    --name "NetSpeedMeter" ^
    --icon=NONE ^
    speedmeter.py

echo.
echo ============================================================
echo Build complete. Find your app at: dist\NetSpeedMeter.exe
echo You can copy that single .exe anywhere and run it directly.
echo ============================================================
pause
