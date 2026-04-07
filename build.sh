#!/bin/bash
# Build script for QC Print Agent macOS Installer

set -e

echo "========================================"
echo "QC Print Agent - macOS Build Script"
echo "========================================"
echo ""

# Check if Python 3 is installed
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is not installed"
    exit 1
fi

# Install/upgrade build dependencies
echo "Installing build dependencies..."
pip3 install --upgrade pyinstaller

# Install project dependencies
echo "Installing project dependencies..."
pip3 install -r requirements.txt

# Clean previous builds
echo "Cleaning previous builds..."
rm -rf build dist

# Build with PyInstaller
echo "Building app bundle..."
pyinstaller print_agent.spec --clean

if [ $? -ne 0 ]; then
    echo "ERROR: Build failed"
    exit 1
fi

# Check if app was created
if [ -d "dist/QC Print Agent.app" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example "dist/QC Print Agent.app/Contents/MacOS/.env.example"
    fi
    echo ""
    echo "========================================"
    echo "Build successful!"
    echo "========================================"
    echo ""
    echo "App bundle: dist/QC Print Agent.app"
    echo ""

    # Create installer package
    echo "Creating installer package..."

    # Create output directory
    mkdir -p installers

    # Copy app bundle and bootstrap config to installers directory
    rm -rf "installers/QC Print Agent.app"
    cp -R "dist/QC Print Agent.app" installers/
    cp .env.example installers/

    # Create README for installer
    cat > installers/README.txt << 'EOF'
QC Print Agent v1.1.8
========================

Installation:
1. Copy QC Print Agent.app to your Applications folder
2. Rename .env.example to .env and configure your settings
3. On a clean machine, open QC Print Agent.app to launch setup automatically
4. Use the packaged binary with --setup to reopen setup or --reset-pairing to reconfigure

For auto-startup on boot, create a Launch Agent:
1. Create ~/Library/LaunchAgents/com.qc.print-agent.plist
2. Add the following content:
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.qc.print-agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Applications/QC Print Agent.app/Contents/MacOS/qc-print-agent</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>

3. Load with: launchctl load ~/Library/LaunchAgents/com.qc.print-agent.plist
EOF

    echo "Installer package created: installers/"
    echo ""
else
    echo "ERROR: App bundle was not created"
    exit 1
fi
