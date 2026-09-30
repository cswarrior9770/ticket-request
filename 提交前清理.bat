@echo off
setlocal
cd /d "%~dp0"
title ticket-request  strip cached DATA

rem ============================================================
rem  One-click: strip the inline `const DATA` blob before push.
rem
rem    double-click            -> strip it (repo stores code only)
rem    double-click + "check"  -> only report the current state
rem    double-click + "restore"-> refill it from data/viz_data.json
rem
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

rem Chinese output from the Python side needs a UTF-8 console
chcp 65001 >nul 2>nul

echo.
%PY% -u "scripts\strip_data.py" %*
echo.
rem double-click keeps the window open; set NO_PAUSE=1 to skip the prompt
rem (useful when calling this .bat from a script / terminal)
if defined NO_PAUSE goto no_pause
pause
:no_pause
exit /b 0

:no_python
echo.
echo   [ERROR] Python 3.10+ was not found on this machine.
echo.
echo   Install it from:  https://www.python.org/downloads/
echo   During setup, tick "Add python.exe to PATH".
echo.
pause
exit /b 1
