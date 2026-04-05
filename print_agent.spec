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

# CRITICAL: Collect Tcl/Tk binaries and data files for GUI
# This ensures the setup wizard works in the bundled executable
if is_windows:
    # Use PyInstaller's built-in data collection for tkinter
    # This automatically finds and bundles Tcl/Tk DLLs and support files
    try:
        tkinter_datas = collect_data_files('tkinter', include_py_files=False)
        datas.extend(tkinter_datas)
    except Exception as e:
        print(f"Warning: Could not collect tkinter data files: {e}")

    # Also try to collect _tkinter module data
    try:
        tk_datas = collect_data_files('_tkinter', include_py_files=False)
        datas.extend(tk_datas)
    except Exception as e:
        print(f"Warning: Could not collect _tkinter data files: {e}")

    # Explicitly collect Tcl/Tk DLLs from common locations
    # This handles different Python distributions
    tcl_dll_names = ['tcl86t.dll', 'tk86t.dll', 'tcl86.dll', 'tk86.dll',
                     'tcl87t.dll', 'tk87t.dll', 'tcl87.dll', 'tk87.dll',
                     'tcl88t.dll', 'tk88t.dll', 'tcl88.dll', 'tk88.dll',
                     'tcl89t.dll', 'tk89t.dll', 'tcl89.dll', 'tk89.dll']

    # Search in standard locations
    search_paths = [
        Path(sys.prefix) / 'DLLs',
        Path(sys.base_prefix) / 'DLLs',
        Path(sys.executable).parent / 'DLLs',
    ]

    for dll_name in tcl_dll_names:
        for search_path in search_paths:
            dll_path = search_path / dll_name
            if dll_path.exists():
                binaries.append((str(dll_path), '.'))
                break

    # Try to collect the tcl directory with supporting files
    tcl_search_paths = [
        Path(sys.prefix) / 'tcl',
        Path(sys.base_prefix) / 'tcl',
        Path(sys.executable).parent / 'tcl',
    ]

    for tcl_path in tcl_search_paths:
        if tcl_path.exists() and tcl_path.is_dir():
            try:
                datas.append((str(tcl_path), 'tcl'))
                break
            except Exception:
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
    console=True,  # Keep console for debugging - can change to False later
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
