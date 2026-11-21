@echo off
setlocal enabledelayedexpansion

echo ========================================
echo   ChestMemory Windows Build Script
echo ========================================

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.12+ not found in PATH.
    exit /b 1
)

if not exist ".venv" (
    echo [1/4] Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo [2/4] Installing dependencies...
python -m pip install --upgrade pip
pip install -r requirements.txt

echo [3/4] Running pytest test suite...
pytest
if errorlevel 1 (
    echo [ERROR] Tests failed. Aborting build.
    exit /b 1
)

echo [4/4] Building dist\ChestMemory.exe via PyInstaller...
pyinstaller --clean --noconfirm ChestMemory.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    exit /b 1
)

echo.
echo [SUCCESS] Built standalone executable: dist\ChestMemory.exe
endlocal
