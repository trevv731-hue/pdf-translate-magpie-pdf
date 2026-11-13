@echo off
rem ---------------------------------------------------------------------------
rem ASCII only, on purpose. cmd.exe parses batch files in the OEM code page;
rem UTF-8 Chinese comments derail the parser mid-line (it starts executing
rem fragments like "venv" or "t" as commands). Keeping this file plain ASCII
rem makes it work no matter what code page the machine is set to.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>&1

rem Pin PYTHONHOME to the bundled runtime, otherwise a system-wide PYTHONPATH
rem leaks in and the embedded interpreter fails to find its stdlib.
set "PYTHONHOME=%~dp0python"
set "PYTHONPATH="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0python\python.exe"
if not exist "%PY%" (
    echo.
    echo   Bundled Python not found:
    echo     %~dp0venv\Scripts\python.exe
    echo     %~dp0python\python.exe
    echo.
    pause
    exit /b 1
)

title PDF Translate Queue
"%PY%" -u "%~dp0pdfq.py" %*

echo.
echo   Press any key to close this window...
pause >nul
endlocal
