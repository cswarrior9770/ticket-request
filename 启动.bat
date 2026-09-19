@echo off
setlocal
cd /d "%~dp0"
title ticket-request  local server

rem ============================================================
rem  One-click launcher:  start local server + open the browser
rem  This project needs NO third-party packages, just Python 3.10+
rem  (content kept ASCII-only on purpose - a .bat with CJK text
rem   can be mis-parsed by cmd under the legacy codepage)
rem ============================================================

set "PY="
set "CHK=import sys;raise SystemExit(0 if sys.hexversion>=0x30A0000 else 1)"

rem ---- 1) official Windows launcher (most reliable) ----
if not defined PY py -3 -c "%CHK%" >nul 2>nul
if not defined PY if not errorlevel 1 set "PY=py -3"

rem ---- 2) whatever "python" resolves to ----
if not defined PY python -c "%CHK%" >nul 2>nul
if not defined PY if not errorlevel 1 set "PY=python"

rem ---- 3) per-user python.org install ----
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" -c "%CHK%" >nul 2>nul
if not defined PY if not errorlevel 1 if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -c "%CHK%" >nul 2>nul
if not defined PY if not errorlevel 1 if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

rem ---- 4) WorkBuddy bundled python (last resort) ----
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" -c "%CHK%" >nul 2>nul
if not defined PY if not errorlevel 1 if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"

if not defined PY goto no_python

echo   Python : %PY%
%PY% -u "scripts\start.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%

:no_python
echo.
echo   [ERROR] Python 3.10+ was not found on this machine.
echo.
echo   Please install Python 3.10 or newer:
echo       https://www.python.org/downloads/
echo   During setup, tick "Add python.exe to PATH".
echo.
echo   Note: this project needs NO extra packages - a plain
echo         Python install is enough (MCP data source uses uvx,
echo         which is optional and only needs uv).
echo.
pause
exit /b 1
