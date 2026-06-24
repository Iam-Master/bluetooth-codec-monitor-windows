@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo No .venv found. Run start.bat first to set up the environment.
    pause
    exit /b 1
)

echo Installing build tools (PyInstaller)...
.venv\Scripts\python -m pip install --quiet -r backend\requirements-dev.txt

echo.
echo Building Codec Monitor.exe ...
.venv\Scripts\python -m PyInstaller backend\codec_monitor.spec --noconfirm --distpath dist --workpath build
if errorlevel 1 (
    echo.
    echo Build FAILED. See errors above.
    pause
    exit /b 1
)

echo.
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup not found - skipping installer build.
    echo The standalone exe is ready at: dist\Codec Monitor.exe
    pause
    exit /b 0
)

echo Building Setup.exe installer...
"%ISCC%" installer.iss
if errorlevel 1 (
    echo.
    echo Installer build FAILED. See errors above.
    pause
    exit /b 1
)

echo.
echo Done. Files ready in dist\:
echo   dist\Codec Monitor.exe        (standalone, no installer)
echo   dist\CodecMonitor-Setup.exe   (installer to share with others)
pause
