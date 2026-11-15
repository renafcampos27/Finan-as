# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH)))  # noqa: F821 - injected by PyInstaller
from scripts.bundle_provenance import validate_binary_provenance


page_hidden_imports = sorted(set(
    collect_submodules("ui.pages")
    + collect_submodules("services")
    + collect_submodules("domain")
    + collect_submodules("ui.components")
    + [
        # Startup-security KDF: PyInstaller cannot see these through the
        # lazy `from argon2.low_level import ...` inside backup_crypto.
        "argon2",
        "argon2.low_level",
        "_argon2_cffi_bindings",
        "numpy",
        "numpy._core._multiarray_umath",
    ]
))

a = Analysis(  # noqa: F821 - injected by PyInstaller
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets')],
    hiddenimports=page_hidden_imports,

    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
binary_provenance = validate_binary_provenance(
    a.binaries,
    source_root=Path(SPECPATH),  # noqa: F821 - injected by PyInstaller
    environment_root=Path(sys.prefix),
    base_python_root=Path(sys.base_prefix),
    windows_root=Path(os.environ["SystemRoot"]),
)
(Path("build") / "BINARY_PROVENANCE.json").write_text(
    json.dumps(binary_provenance, indent=2), encoding="utf-8"
)
pyz = PYZ(a.pure)  # noqa: F821 - injected by PyInstaller

exe = EXE(  # noqa: F821 - injected by PyInstaller
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LiquidGlass',
    version='build/version_info.txt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(  # noqa: F821 - injected by PyInstaller
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LiquidGlass',
)
