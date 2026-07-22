@echo off
REM ============================================================
REM  SaveSync - install dependencies with NO network access
REM  Installs every requirement from the local offline_deps\
REM  folder (populated beforehand with download_offline_deps.bat
REM  on a machine with the same OS and Python version).
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist offline_deps (
    echo [!!] offline_deps\ folder not found.
    echo      Run download_offline_deps.bat on an online machine first.
    pause
    exit /b 1
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m pip install --no-index --find-links=offline_deps -r requirements.txt
) else (
    python -m pip install --no-index --find-links=offline_deps -r requirements.txt
)
set EXITCODE=%errorlevel%

echo.
if %EXITCODE%==0 (
    echo [OK] All dependencies installed offline. Run:  python main.py
) else (
    echo [!!] Offline install failed ^(exit code %EXITCODE%^).
    echo      Check that offline_deps\ was populated on a machine with
    echo      the SAME OS and Python version as this one.
)
pause
exit /b %EXITCODE%
