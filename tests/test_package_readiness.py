from __future__ import annotations

import json
import platform
import re
import subprocess
from importlib.metadata import version
from pathlib import Path

import cryptography
import PySide6
import pytest

from utils.package_self_test import write_package_self_test_report
from utils.version import APP_VERSION_LABEL


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _pinned_version(path: Path, package: str) -> str:
    match = re.search(
        rf"^{re.escape(package)}==([^\s]+)$",
        path.read_text(encoding="utf-8"),
        flags=re.MULTILINE | re.IGNORECASE,
    )
    assert match is not None
    return match.group(1)


def test_runtime_and_build_dependencies_match_the_pinned_versions() -> None:
    runtime = PROJECT_ROOT / "requirements.txt"
    development = PROJECT_ROOT / "requirements-dev.txt"

    python_version = tuple(map(int, platform.python_version_tuple()))
    assert python_version[:2] == (3, 14)
    assert python_version >= (3, 14, 7)
    assert PySide6.__version__ == _pinned_version(runtime, "PySide6")
    assert cryptography.__version__ == _pinned_version(runtime, "cryptography")
    assert version("PyInstaller") == _pinned_version(development, "PyInstaller")
    assert version("pip-audit") == _pinned_version(development, "pip-audit")
    assert version("pytest") == _pinned_version(development, "pytest")
    assert version("ruff") == _pinned_version(development, "ruff")


def test_packaged_runtime_self_test_loads_pages_and_reopens_database(
    tmp_path: Path,
) -> None:
    from PySide6.QtWidgets import QApplication

    qapp = QApplication.instance() or QApplication([])
    target = tmp_path / "package-self-test.json"
    report = write_package_self_test_report(target)

    assert report["ok"] is True
    assert report["database_quick_check"] == "ok"
    assert report["page_exports_loaded"] == len(report["page_exports"])
    assert report["page_exports_loaded"] >= 25
    assert report["python"] == platform.python_version()
    assert report["pyside6"] == PySide6.__version__
    assert report["cryptography"] == cryptography.__version__
    pdf = report["functional_checks"]["pdf_preview"]
    assert pdf["passed"] is True
    assert pdf["document_pages"] == 1
    assert (pdf["render_width"], pdf["render_height"]) == (240, 320)
    assert pdf["rendered_marker_pixels"] >= 20
    assert pdf["viewer_marker_pixels"] >= 20
    assert pdf["viewer_shown"] is True
    assert re.fullmatch(r"[a-f0-9]{64}", pdf["pdf_sha256"])
    assert {"QtPdf", "QtPdfWidgets"} <= report["bundle_module_paths"].keys()
    assert json.loads(target.read_text(encoding="utf-8")) == report
    qapp.processEvents()


def test_build_checks_dependencies_executes_package_and_cleans_work_folders() -> None:
    build = (PROJECT_ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")

    assert "-m pip check" in build
    assert '"--package-self-test"' in build
    assert "LIQUIDGLASS_SELF_TEST_REPORT" in build
    assert "requires_python_on_target = $false" in build
    assert "if ([bool]$runtimeInfo.free_threaded)" in build
    assert 'Remove-GeneratedPath "build"' in build
    assert 'Remove-GeneratedPath "dist"' in build
    assert "RELEASE_MANIFEST.json" in build
    assert '"_internal\\sqlite3.dll"' in build
    assert '"_internal\\PySide6\\Qt6Multimedia.dll"' in build
    assert '"_internal\\PySide6\\plugins\\platforms\\qwindows.dll"' in build
    assert '"INSTALAR_FINANCAS_PESSOAIS.bat"' in build
    assert '"ABRIR_FINANCAS_PESSOAIS.bat"' in build
    assert '"pytest"' in build
    assert '"ruff"' in build
    assert '"pip-audit"' in build
    assert "$manifest.package_integrity" in build
    assert "packageEntries" in build
    assert "$installedTestRoot\\LiquidGlass.exe" in build
    assert "$manifest.installation.install_test.passed = $true" in build


def test_pdf_self_test_rejects_a_blank_render(tmp_path, monkeypatch) -> None:
    from PySide6.QtGui import QImage
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication
    from utils.package_self_test import _pdf_preview_check

    _app = QApplication.instance() or QApplication([])
    blank = QImage(240, 320, QImage.Format.Format_ARGB32)
    blank.fill(0xFFFFFFFF)
    monkeypatch.setattr(QPdfDocument, "render", lambda *_args: blank)
    with pytest.raises(RuntimeError, match="motor PDF não desenhou"):
        _pdf_preview_check(tmp_path)


def test_installation_docs_describe_the_complete_portable_folder() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    guide = (PROJECT_ROOT / "COMO_INSTALAR_NOUTRO_PC.txt").read_text(encoding="utf-8")

    assert any(APP_VERSION_LABEL in line for line in readme.splitlines()[0:12])
    assert "não precisa de\nPython nem de pip" in readme
    assert "copiar apenas `LiquidGlass.exe` não funciona" in readme
    assert "Extrai TODO o conteúdo" in guide
    assert "Não movas apenas o\nEXE" in guide
    assert "INSTALAR_FINANCAS_PESSOAIS.bat" in readme
    assert "não deve ser aberto\ncomo administrador" in readme
    assert "%LOCALAPPDATA%\\Programs\\FinancasPessoaisLiquidGlass" in guide
    assert "Não executa pip" in guide


def test_per_user_installer_is_safe_atomic_and_parses_in_windows_powershell() -> None:
    installer = (
        PROJECT_ROOT / "packaging" / "windows" / "INSTALAR_FINANCAS_PESSOAIS.ps1"
    )
    wrapper = PROJECT_ROOT / "packaging" / "windows" / "INSTALAR_FINANCAS_PESSOAIS.bat"
    portable = PROJECT_ROOT / "packaging" / "windows" / "ABRIR_FINANCAS_PESSOAIS.bat"
    installer_text = installer.read_text(encoding="utf-8")
    wrapper_text = wrapper.read_text(encoding="utf-8")
    portable_text = portable.read_text(encoding="utf-8")

    assert "Get-FileHash" in installer_text
    assert "RELEASE_MANIFEST.json" in installer_text
    assert "package_integrity" in installer_text
    assert "integrityEntries" in installer_text
    assert "actualFiles.Count -ne $listedPaths.Count" in installer_text
    assert "requires_python_on_target" in installer_text
    assert "FinancasPessoaisLiquidGlass" in installer_text
    assert 'Join-Path $env:LOCALAPPDATA "FinancasPessoais"' in installer_text
    assert ".installing-" in installer_text
    assert ".previous-" in installer_text
    assert "Move-Item" in installer_text
    assert "WScript.Shell" in installer_text
    assert "pip install" not in installer_text
    assert "ExecutionPolicy Bypass" in wrapper_text
    assert "runas" not in wrapper_text.casefold()
    assert "_internal\\python314.dll" in portable_text

    escaped = str(installer).replace("'", "''")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"$null = [scriptblock]::Create((Get-Content -LiteralPath '{escaped}' -Raw))",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_source_launchers_select_python_314_and_keep_logs_outside_source() -> None:
    standard = (PROJECT_ROOT / "run_windows_liquid_glass.bat").read_text(
        encoding="utf-8"
    )
    debug = (PROJECT_ROOT / "run_windows_liquid_glass_debug.bat").read_text(
        encoding="utf-8"
    )
    silent = (PROJECT_ROOT / "run_liquid_glass_sem_consola.vbs").read_text(
        encoding="utf-8"
    )
    launcher = (PROJECT_ROOT / "financas_liquid_glass_launcher.pyw").read_text(
        encoding="utf-8"
    )

    assert "pyw.exe -3.14" in standard
    assert "py -3.14" in debug
    assert "pyw.exe -3.14" in silent
    assert "%LOCALAPPDATA%" in silent
    assert 'STATE_ROOT / "logs"' in launcher
    assert 'BASE / "logs"' not in launcher
