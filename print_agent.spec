# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for QC Print Agent.

Builds a standalone executable for Windows (.exe) or macOS (.app).

IMPORTANT: Uses --onedir mode (directory with supporting files)
instead of --onefile (single exe). This is required for Tkinter to work
because Tcl/Tk needs access to its supporting files at runtime.
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

# Icon paths
if is_windows:
    icon_file = 'assets/icon.ico' if Path('assets/icon.ico').exists() else None
elif is_macos:
    icon_file = 'assets/icon.icns' if Path('assets/icon.icns').exists() else None

# Collect all hidden imports
hiddenimports = [
    'keyring.backends',
    'keyring.backends.Windows',
    'keyring.backends.kwallet',
    'requests',
    'urllib3',
    # Tkinter - collect ALL submodules
    *collect_submodules('tkinter'),
]

# Data files to include
datas = [
    ('.env.example', '.'),
]

# Binary files to include
binaries = []

# CRITICAL: Tcl/Tk support for GUI
# PyInstaller's hooks should handle this, but we explicitly collect them
if is_windows:
    # Collect tkinter data files (includes Tcl/Tk DLLs and supporting files)
    try:
        tkinter_datas = collect_data_files('tkinter', include_py_files=False)
        datas.extend(tkinter_datas)
        print(f"[PyInstaller] Collected {len(tkinter_datas)} tkinter data files")
    except Exception as e:
        print(f"[PyInstaller] Warning: Could not collect tkinter data: {e}")

    # Also collect _tkinter
    try:
        tk_datas = collect_data_files('_tkinter', include_py_files=False)
        datas.extend(tk_datas)
        print(f"[PyInstaller] Collected {len(tk_datas)} _tkinter data files")
    except Exception as e:
        print(f"[PyInstaller] Warning: Could not collect _tkinter data: {e}")

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

# NOTE: Using onedir mode (directory with exe + supporting files)
# This is REQUIRED for Tkinter to work because Tcl/Tk needs access
# to its supporting files at runtime.
exe = EXE(
    pyz,
    a.scripts,
    [],  # Exclude binaries from exe (they go in the folder)
    exclude_binaries=True,  # CRITICAL: onedir mode
    name='qc-print-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Keep console for now (can change to False after testing)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file,
)

# Collect all binaries and data files into the dist folder
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='qc-print-agent',
)

# macOS: Create .app bundle from the collected folder
# Note: We use 'coll' not 'exe' because onedir mode has exclude_binaries=True
if is_macos:
    app = BUNDLE(
        coll,
        name='QC Print Agent.app',
        icon=icon_file,
        bundle_identifier='com.qc.print-agent',
        info_plist={
            'CFBundleName': 'QC Print Agent',
            'CFBundleDisplayName': 'QC Print Agent',
            'CFBundleVersion': '1.1.0',
            'CFBundleShortVersionString': '1.1.0',
            'LSUIElement': True,  # Run as background agent (no dock icon)
        },
    )
