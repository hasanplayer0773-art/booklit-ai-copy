# -*- mode: python ; coding: utf-8 -*-
"""
Booklet_AI Desktop — PyInstaller Specification File (STEP-4.1)

Packaging Architecture:
- `--onedir` mode (via COLLECT): Avoids multi-second temp-extraction delay
  of `--onefile` on every launch and simplifies partial-file updates and delta-free
  package replacements without antivirus quarantine risks (AC-4.1.4).
- External prerequisites: InDesign is NOT bundled (relies on licensed COM install).
- Bundled assets: assets/ (JSX scripts, XSLT), migrations/ (*.sql), and sqlite-vec DLLs.
"""
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

block_cipher = None

# Determine project and src directories reliably regardless of invocation cwd
current_dir = Path(SPECPATH).resolve()
if (current_dir / "src" / "app").exists():
    src_dir = current_dir / "src"
    root_dir = current_dir
elif (current_dir / "app").exists():
    src_dir = current_dir
    root_dir = current_dir.parent
else:
    src_dir = current_dir / "src"
    root_dir = current_dir

# 1. Bundled Data Files
datas = []
assets_dir = src_dir / "assets"
if assets_dir.exists():
    datas.append((str(assets_dir), "assets"))

migrations_dir = src_dir / "migrations"
if migrations_dir.exists():
    datas.append((str(migrations_dir), "migrations"))
elif (root_dir / "migrations").exists():
    datas.append((str(root_dir / "migrations"), "migrations"))

# Include sqlite-vec extension data
try:
    datas += collect_data_files("sqlite_vec")
except Exception:
    pass

# Include weasyprint and ziamath data files (CSS, MathML fonts)
try:
    datas += collect_data_files("weasyprint")
except Exception:
    pass
try:
    datas += collect_data_files("ziamath")
except Exception:
    pass
try:
    datas += collect_data_files("ziafont")
except Exception:
    pass

# 2. Bundled Dynamic Libraries (DLLs)
binaries = []
try:
    binaries += collect_dynamic_libs("sqlite_vec")
except Exception:
    pass

# 3. Hidden Imports
hiddenimports = [
    # Core app submodules
    "app",
    "app.ai",
    "app.ai.classifier",
    "app.ai.client",
    "app.ai.embedding_client",
    "app.ai.question_indexer",
    "app.ai.schemas",
    "app.automation",
    "app.automation.com_supervisor",
    "app.automation.geometry",
    "app.automation.indesign_precondition",
    "app.automation.job_protocol",
    "app.automation.job_retry",
    "app.automation.pagination_sync",
    "app.automation.style_dispatch",
    "app.automation.styles_schema",
    "app.automation.template_preflight",
    "app.core",
    "app.core.docx_extractor",
    "app.core.exceptions",
    "app.core.models",
    "app.core.pdf_extractor",
    "app.core.pricing_engine",
    "app.core.pricing_models",
    "app.core.review_pdf",
    "app.core.text_integrity",
    "app.state",
    "app.state.app_store",
    "app.state.document_store",
    "app.ui",
    "app.ui.ai_settings",
    "app.ui.error_translation",
    "app.ui.problem_report",
    "app.ui.report_redaction",
    "app.utils",
    "app.utils.auto_recovery",
    "app.utils.db",
    "app.utils.logging_config",
    "app.utils.secrets_manager",
    "app.utils.updater",
    # Third-party dependencies
    "sqlite_vec",
    "pydantic",
    "pydantic_core",
    "httpx",
    "win32com",
    "win32com.client",
    "pythoncom",
    "docx",
    "fitz",
    "lxml",
    "keyring",
    "psutil",
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "weasyprint",
    "ziamath",
    "ziafont",
    "cairocffi",
    "cffi",
]

try:
    hiddenimports += collect_submodules("app")
except Exception:
    pass

# 4. PyInstaller Analysis
a = Analysis(
    [str(src_dir / "main.py")],
    pathex=[str(src_dir), str(root_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher,
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # Crucial for --onedir distribution mode
    name="Booklet_AI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX is disabled to prevent antivirus false-positives (AC-4.1.4)
    console=False,  # Windows GUI application (no persistent console window)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# Crucial: COLLECT produces the directory-based (--onedir) bundle
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Booklet_AI",
)
