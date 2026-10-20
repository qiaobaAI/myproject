@echo off
chcp 936 >nul
set "PYTHONUTF8=0"
set "PYTHONIOENCODING=gbk:replace"
set "PATH=%~dp0tools\ffmpeg\bin;%PATH%"

echo ============================================
echo   Local Video -^> AI Rich Notes (output to output\, index.html)
echo   Video/screen recording: start llama-server first via the launcher bat
echo   Also supports dropping a subtitle/transcript .txt (no ASR needed)
echo ============================================

set "IN=%~1"
set "CALLED_WITH_ARG="
if defined IN set "CALLED_WITH_ARG=1"
if not defined IN (
    set /p "IN=Enter path of video / folder / subtitle .txt: "
)
if not defined IN (
    echo No path given, exit.
    if not defined CALLED_WITH_ARG pause
    exit /b 1
)
set "IN=%IN:"=%"
if not exist "%IN%" (
    echo Path not found: "%IN%"
    if not defined CALLED_WITH_ARG pause
    exit /b 1
)

echo [*] Checking local LLM service (localhost:8000)...
curl -s -m 5 http://localhost:8000/v1/models >nul 2>&1
if errorlevel 1 (
    echo.
    echo [WARN] Cannot reach localhost:8000 - LLM service may be OFF.
    echo   If processing fails below, run the launcher bat in
    echo   F:\llama-b9828-bin-win-cuda-13.3-x64 , then rerun this script.
    echo.
) else (
    echo [OK] Service online, processing...
)

cd /d "%~dp0"
call ".venv\Scripts\python.exe" make_notes.py "%IN%"
set RC=%errorlevel%
echo.
if not "%RC%"=="0" (
    echo [TIP] Finished with errors (exit code %RC%). See log above or output\run_*.log
) else (
    echo [OK] All done. Notes are in output\ , open index.html in browser.
)
echo ========== Press any key to exit ==========
if not defined CALLED_WITH_ARG pause >nul
