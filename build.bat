@echo off
setlocal
title Agent-Legacy single-file build
cd /d "%~dp0"

if exist "venv\Scripts\python.exe" (
    set PY=venv\Scripts\python.exe
) else (
    set PY=python
)

rem 1. Make sure PyInstaller is available
"%PY%" -m pip show pyinstaller >nul 2>nul || "%PY%" -m pip install pyinstaller -q

rem 2. Build the single-file binary (uses UPX from tools\ if present, ~2.5MB smaller)
set "UPXFLAG="
if exist "tools\upx-4.2.4-win64\upx.exe" set "UPXFLAG=--upx-dir tools\upx-4.2.4-win64"
"%PY%" -m PyInstaller --clean --noconfirm %UPXFLAG% agent-legacy.spec
if errorlevel 1 (
    echo BUILD FAILED
    exit /b 1
)

echo.
echo  ================================================
echo    Built: dist\agent-legacy.exe
echo    Ship:  agent-legacy.exe + .env sidecar
echo    Test:  dist\agent-legacy.exe --smoke
echo  ================================================
endlocal
