@echo off
setlocal
REM Telegram <-> Claude Code bridge.
REM Keeps the bridge running, restarting it if it exits unexpectedly.
REM Logs go to bridge.log in this folder as well as this window.

cd /d "%~dp0"
title Telegram <-> Claude Code

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (
    python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo ERROR: Python 3 was not found.
    echo        Install it from https://www.python.org/downloads/ ^(tick "Add to PATH"^),
    echo        or edit this file to set PY to your python.exe.
    pause
    exit /b 1
)

if not exist "bridge.py" (
    echo ERROR: bridge.py is missing from "%~dp0".
    pause
    exit /b 1
)

if not exist "config.json" (
    echo No config.json yet - running first-time setup.
    echo.
    %PY% bridge.py --setup
    if errorlevel 1 (
        echo.
        echo ERROR: setup did not complete.
        pause
        exit /b 1
    )
    echo.
)

set /a RESTARTS=0

:loop
%PY% bridge.py %*
set "RC=%errorlevel%"

if "%RC%"=="0" goto :clean_exit
if "%RC%"=="2" goto :config_problem

set /a RESTARTS+=1
if %RESTARTS% GEQ 5 (
    echo.
    echo ERROR: the bridge crashed 5 times in a row. Giving up.
    echo        See bridge.log for the last errors.
    pause
    exit /b %RC%
)

echo.
echo Bridge exited with code %RC%. Restarting in 10 seconds ^(Ctrl+C to stop^)...
timeout /t 10 /nobreak >nul
goto :loop

:config_problem
echo.
echo ERROR: the bridge refused to start - see the message above.
echo        Usually a missing token or an empty allowed_user_ids list.
echo        Fix it with:  %PY% bridge.py --setup
pause
exit /b 2

:clean_exit
echo.
echo Bridge stopped.
exit /b 0
