@echo off
setlocal
cd /d "%~dp0"
set "PYTHONHOME=%~dp0python"
set "PYTHONPATH="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PY=%~dp0python\python.exe"
if not exist "%PY%" set "PY=%~dp0venv\Scripts\python.exe"

"%PY%" -u "%~dp0select_model.py"
endlocal
