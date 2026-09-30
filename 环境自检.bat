@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Prefer the portable Python bundled in this folder, else fall back to system Python.
set PY=%~dp0python\python.exe
if not exist "%PY%" set PY=python

"%PY%" "%~dp0merge_av.py" --check
echo.
pause
