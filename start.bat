@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First-time setup: creating isolated Python environment...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: Could not create venv. Make sure Python 3.10+ is installed
        echo and on your PATH. Try running: python --version
        echo.
        pause
        exit /b 1
    )
    .venv\Scripts\python -m pip install --quiet --upgrade pip
    .venv\Scripts\python -m pip install --quiet -r backend\requirements.txt
    echo Setup complete.
    echo.
)

start "" .venv\Scripts\pythonw.exe backend\app.py
