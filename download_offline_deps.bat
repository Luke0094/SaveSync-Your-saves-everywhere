@echo off
REM ============================================================
REM  SaveSync - populate the offline dependency folder
REM  Downloads every requirement (wheels for THIS platform and
REM  Python version) into offline_deps\ for later offline install.
REM  Run this once from an ONLINE machine with the same OS/Python
REM  as the target machine.
REM ============================================================
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m pip download -r requirements.txt -d offline_deps
) else (
    python -m pip download -r requirements.txt -d offline_deps
)
set EXITCODE=%errorlevel%

echo.
if %EXITCODE%==0 (
    echo [OK] offline_deps\ populated. Copy the whole project folder
    echo      to the offline machine and run install_offline_deps.bat
) else (
    echo [!!] Download failed ^(exit code %EXITCODE%^).
)
pause
exit /b %EXITCODE%
