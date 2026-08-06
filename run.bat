@echo off
setlocal
title AI Operations Center
cd /d "%~dp0"

rem Use the project venv if it exists, else fall back to system python.
if exist "venv\Scripts\python.exe" (
    set PY=venv\Scripts\python.exe
) else (
    set PY=python
)

rem Make the project importable from any directory.
set "PYTHONPATH=%~dp0"

rem Optional: --plain uses the simple streaming CLI instead of the TUI.
if /I "%~1"=="--plain" (
    shift
    "%PY%" -m agent.cli %*
    goto :eof
)

if "%~1"=="" goto :prompt

"%PY%" -m agent.tui %*
goto :eof

:prompt
echo.
echo  ======================================================
echo    Universal AI Operations Center
echo    Type a goal and watch the whole team work live.
echo    Sample: research AI agents and write a report
echo    Use --plain for the simple log view
echo  ======================================================
echo.
"%PY%" -m agent.tui
goto :eof

endlocal
