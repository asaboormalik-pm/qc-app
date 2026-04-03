@echo off
REM Build script for QC Print Agent Windows Installer

echo ========================================
echo QC Print Agent - Windows Build Script
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    exit /b 1
)

REM Install/upgrade build dependencies
echo Installing build dependencies...
pip install --upgrade pyinstaller

REM Install project dependencies
echo Installing project dependencies...
pip install -r requirements.txt

REM Clean previous builds
echo Cleaning previous builds...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM Build with PyInstaller
echo Building executable...
pyinstaller print_agent.spec --clean

if errorlevel 1 (
    echo ERROR: Build failed
    exit /b 1
)

REM Check if executable was created
if exist dist\qc-print-agent.exe (
    echo.
    echo ========================================
    echo Build successful!
    echo ========================================
    echo.
    echo Executable: dist\qc-print-agent.exe
    echo.

    REM Create a simple installer script
    echo Creating installer package...

    REM Create output directory
    if not exist installers mkdir installers

    REM Copy executable to installers directory
    copy dist\qc-print-agent.exe installers\ >nul
    copy .env.example installers\.env.example >nul

    REM Create README for installer
    echo QC Print Agent v1.0.0 > installers\README.txt
    echo ======================== >> installers\README.txt
    echo. >> installers\README.txt
    echo Installation: >> installers\README.txt
    echo 1. Copy qc-print-agent.exe to a folder on your computer >> installers\README.txt
    echo 2. Rename .env.example to .env and configure your settings >> installers\README.txt
    echo 3. Run qc-print-agent.exe --setup to pair with your warehouse >> installers\README.txt
    echo 4. Run qc-print-agent.exe to start the agent >> installers\README.txt
    echo. >> installers\README.txt
    echo For auto-startup on boot, create a Windows Scheduled Task or >> installers\README.txt
    echo place a shortcut in your Startup folder. >> installers\README.txt

    echo Installer package created: installers\
    echo.
) else (
    echo ERROR: Executable was not created
    exit /b 1
)

pause
