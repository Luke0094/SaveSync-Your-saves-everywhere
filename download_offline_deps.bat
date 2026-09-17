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
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

%PY% -m pip download -r requirements.txt -d offline_deps
set EXITCODE=%errorlevel%

REM langdetect has no wheel on PyPI, only a source dist -- pip download
REM would otherwise vendor the .tar.gz, and installing FROM an sdist needs
REM setuptools as a build dependency at install time, which is exactly the
REM one thing --no-index --find-links can't fall back to PyPI for. Building
REM the (universal, pure-Python) wheel here instead sidesteps that: the
REM target machine then just installs a wheel, no build step, no setuptools
REM needed. Same fix tests\download_offline_deps.bat already uses.
if not %EXITCODE%==0 goto skipwheel
del /q offline_deps\langdetect-*.tar.gz >nul 2>nul
%PY% -m pip wheel langdetect --no-deps -w offline_deps
set EXITCODE=%errorlevel%
:skipwheel

echo.
if %EXITCODE%==0 (
    echo [OK] offline_deps\ populated. Copy the whole project folder
    echo      to the offline machine and run install_offline_deps.bat
) else (
    echo [!!] Download failed ^(exit code %EXITCODE%^).
)
pause
exit /b %EXITCODE%
