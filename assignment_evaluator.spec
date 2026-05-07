# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Assignment Evaluator
Build with:  pyinstaller assignment_evaluator.spec
Output:      dist/AssignmentEvaluator.exe  (single file, no console window)
"""

import sys
from pathlib import Path
import pymupdf  # ensures pymupdf is importable at spec-parse time

block_cipher = None

# ---------------------------------------------------------------------------
# Collect all data files that PyMuPDF ships (fonts, colour-profiles, etc.)
# ---------------------------------------------------------------------------
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

pymupdf_datas    = collect_data_files("pymupdf")
pymupdf_binaries = collect_dynamic_libs("pymupdf")

# numpy C-extensions must be fully collected or submodules like
# numpy._core._exceptions will be missing at runtime.
numpy_hidden = collect_submodules("numpy")

a = Analysis(
    ["assignment_evaluator/__main__.py"],
    pathex=["."],
    binaries=pymupdf_binaries,
    datas=pymupdf_datas,
    hiddenimports=numpy_hidden + [
        # PyMuPDF internals
        "pymupdf",
        "pymupdf.utils",
        # google-generativeai pulls in a lot of optional sub-modules
        "google.generativeai",
        "google.ai.generativelanguage_v1beta",
        "google.api_core",
        "google.auth",
        "google.auth.transport.requests",
        "grpc",
        # pdf2image / Pillow
        "pdf2image",
        "PIL._tkinter_finder",
        # pandas / openpyxl
        "openpyxl",
        "pandas",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="AssignmentEvaluator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no black terminal window behind the GUI
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
