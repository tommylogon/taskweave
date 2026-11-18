@echo off
setlocal enabledelayedexpansion
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

if not "%~1"=="" (
    set "TARGET_DIR=%~1"
    if "!TARGET_DIR:~-1!"=="\" set "TARGET_DIR=!TARGET_DIR:~0,-1!"
    set "TASKWEAVE_ROOT=!TARGET_DIR!"
)

set "PY=C:\ProgramData\miniconda3\python.exe"
if not exist "%PY%" set "PY=python"

set "PYTHONPATH=%SCRIPT_DIR%\src;%PYTHONPATH%"
"%PY%" -m taskweave.mcp_server
