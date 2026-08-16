@echo off
setlocal
set ROOT=%~dp0
set PORT=8787

rem Start the Responses-API bridge (only if not already running)
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:%PORT%/ | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    start "agent-legacy-codex-bridge" /min cmd /c "cd /d %ROOT% && venv\Scripts\pythonw.exe -m agent.codex_bridge"
    timeout /t 2 /nobreak >nul
)

codex --profile tokenrouter %*
