@echo off
setlocal
title Agent-Legacy
cd /d "%~dp0"

rem Use the project venv if it exists, else fall back to system python.
if exist "venv\Scripts\python.exe" (
    set PY=venv\Scripts\python.exe
) else (
    set PY=python
)

rem Make the project importable from any directory.
set "PYTHONPATH=%~dp0"

rem Optional view flags:
rem   --app   -> Textual dashboard (default)
rem   --plain -> simple streaming CLI
rem   --rich  -> original Rich dashboard (fallback)
if /I "%~1"=="--plain" (
    shift
    "%PY%" -m agent.cli %*
    goto :eof
)
if /I "%~1"=="--rich" (
    shift
    "%PY%" -m agent.tui %*
    goto :eof
)
if /I "%~1"=="--app" shift

if "%~1"=="" goto :prompt

"%PY%" -m agent.tapp %*
goto :eof

:prompt
echo.
echo  ======================================================
echo    Agent-Legacy
echo    Cost-aware multi-agent deep research — routed to the cheapest capable model.
echo    Sample: research AI agents and write a report
echo    Flags: --plain (simple log), --rich (old Rich UI)
echo  ======================================================
echo.
"%PY%" -m agent.tapp
goto :eof

endlocal
