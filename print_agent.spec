# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for QC Print Agent.

Builds a standalone executable for Windows (.exe) or macOS (.app).

Usage:
    Windows: pyinstaller print_agent.spec
    macOS:   pyinstaller --windowed print_agent.spec
"""

import sys
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Platform-specific settings
is_windows = sys.platform.startswith('win')
is_macos = sys.platform.startswith('darwin')

block_cipher = None
icon_file = None

# Icon paths (optional - add your icons later)
if is_windows:
    icon_file = 'assets/icon.ico' if Path('assets/icon.ico').exists() else None
elif is_macos:
    icon_file = 'assets/icon.icns' if Path('assets/icon.icns').exists() else None

# Collect all hidden imports
hiddenimports = [
    'keyring.backends',
    'keyring.backends._OS_X_API',
    'keyring.backends.SecretService',
    'keyring.backends.Windows',
    'keyring.backends.kwallet',
    'requests',
    'urllib3',
    # Tkinter for setup wizard - collect all submodules
    *collect_submodules('tkinter'),
]

# Data files to include (templates, configs, etc.)
datas = [
    ('.env.example', '.'),
]

# Binary files to include
binaries = []

# CRITICAL: Collect Tcl/Tk data files for Windows
# This ensures the GUI works in the bundled executable
if is_windows:
    try:
        tkinter_datas = collect_data_files('tkinter')
        datas.extend(tkinter_datas)
    except Exception:
        # If collect_data_files fails, try manual collection
        pass

a = Analysis(
    ['print_agent.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 'tkinter',  # INCLUDED - needed for setup wizard
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='qc-print-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Show console to debug GUI issues
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file,
)

# macOS: Create .app bundle (in addition to standalone executable)
if is_macos:
    app = BUNDLE(
        exe,
        name='QC Print Agent.app',
        icon=icon_file,
        bundle_identifier='com.qc.print-agent',
        info_plist={
            'CFBundleName': 'QC Print Agent',
            'CFBundleDisplayName': 'QC Print Agent',
            'CFBundleVersion': '1.0.0',
            'CFBundleShortVersionString': '1.0.0',
            'LSUIElement': True,  # Run as background agent (no dock icon)
        },
    )
