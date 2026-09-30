@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ffmpeg is NOT shipped in this repo: a single binary is 160MB, which exceeds
rem the 100MB per-file limit on GitHub and Gitee. Fetch it once after cloning.
if not exist "%~dp0bin\ffmpeg.exe" goto noffmpeg

rem Prefer the portable Python bundled in this folder, else fall back to system Python.
set PY=%~dp0python\pythonw.exe
if not exist "%PY%" set PY=pythonw

start "" "%PY%" "%~dp0merge_av.py"
exit /b 0

:noffmpeg
echo.
echo   [av-toolbox] bin\ffmpeg.exe not found.
echo.
echo   This repo does not include ffmpeg (each binary is 160MB, over the
echo   100MB per-file limit on GitHub / Gitee). Download it once:
echo.
echo       python fetch_ffmpeg.py
echo.
echo   Behind a proxy? Add:  --proxy http://127.0.0.1:17891
echo.
pause
exit /b 1
