@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "PY=C:\ProgramData\miniconda3\python.exe"
if not exist "%PY%" set "PY=python"

set "PYTHONPATH=%SCRIPT_DIR%\src;%PYTHONPATH%"
"%PY%" -m taskweave.cli %*
exit /b %errorlevel%
