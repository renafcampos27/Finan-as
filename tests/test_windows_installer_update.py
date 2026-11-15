"""Actual Windows installer exercised only on synthetic, never-launched files."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows installer")
PROJECT = Path(__file__).resolve().parents[1]
REQUIRED_RUNTIME = (
    "_internal/python314.dll",
    "_internal/VCRUNTIME140.dll",
    "_internal/VCRUNTIME140_1.dll",
    "_internal/sqlite3.dll",
    "_internal/_sqlite3.pyd",
    "_internal/PySide6/Qt6Core.dll",
    "_internal/PySide6/Qt6Gui.dll",
    "_internal/PySide6/Qt6Widgets.dll",
    "_internal/PySide6/Qt6Network.dll",
    "_internal/PySide6/Qt6Multimedia.dll",
    "_internal/PySide6/plugins/platforms/qwindows.dll",
    "_internal/cryptography/hazmat/bindings/_rust.pyd",
    "_internal/assets/style.qss",
)


def _package(root: Path, version: str) -> Path:
    root.mkdir(parents=True)
    content = {
        name: b"Synthetic runtime fixture; never executable.\n"
        for name in REQUIRED_RUNTIME
    }
    content["LiquidGlass.exe"] = f"Synthetic {version}; never execute.\n".encode()
    content["INSTALAR_FINANCAS_PESSOAIS.ps1"] = (
        PROJECT / "packaging/windows/INSTALAR_FINANCAS_PESSOAIS.ps1"
    ).read_bytes()
    content["APLICAR_ATUALIZACAO.ps1"] = (
        PROJECT / "packaging/windows/APLICAR_ATUALIZACAO.ps1"
    ).read_bytes()
    for name, data in content.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = {
        "version": version,
        "target": {
            "operating_system": "Windows", "bits": 64,
            "requires_python_on_target": False,
        },
        "self_test": {"passed": True},
        "installation": {"dependencies_embedded": True, "requires_admin": False},
        "executable_sha256": hashlib.sha256(content["LiquidGlass.exe"]).hexdigest(),
        "package_integrity": {
            "algorithm": "SHA-256", "file_count": len(content),
            "entries": [
                {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in sorted(content.items())
            ],
        },
    }
    (root / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _run_installer(package: Path, install: Path, local: Path) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(local)
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    return subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(package / "INSTALAR_FINANCAS_PESSOAIS.ps1"),
         "-InstallRoot", str(install), "-NoShortcuts", "-NoLaunch", "-NonInteractive"],
        env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW, check=False,
    )


def _inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def test_real_installer_installs_and_upgrades_without_changing_synthetic_data(tmp_path: Path) -> None:
    local = tmp_path / "local app data"
    data = local / "FinancasPessoais" / "dados"
    data.mkdir(parents=True)
    (data / "synthetic-finance.sqlite3").write_bytes(b"Synthetic data preservation sentinel")
    (data / "synthetic-preferences.json").write_text('{"synthetic":true}', encoding="utf-8")
    original_data = _inventory(data)
    install = local / "Programs" / "FinancasPessoaisLiquidGlass"
    first = _package(tmp_path / "download first ' & ç", "3.49.0")
    result = _run_installer(first, install, local)
    assert result.returncode == 0, (result.stdout + result.stderr).decode(errors="replace")
    assert _inventory(install) == _inventory(first)
    second = _package(tmp_path / "download second ' & ç", "3.50.0")
    result = _run_installer(second, install, local)
    assert result.returncode == 0, (result.stdout + result.stderr).decode(errors="replace")
    assert _inventory(install) == _inventory(second)
    assert _inventory(data) == original_data
    assert not list(install.parent.glob("*.installing-*"))
    assert not list(install.parent.glob("*.previous-*"))


@pytest.mark.parametrize("mutation", ["changed", "missing", "extra", "wrong_target"])
def test_real_installer_rejects_invalid_package_before_replacing_installation(
    tmp_path: Path, mutation: str,
) -> None:
    local = tmp_path / "local"
    install = local / "Programs" / "FinancasPessoaisLiquidGlass"
    install.mkdir(parents=True)
    (install / "LiquidGlass.exe").write_bytes(b"Previous synthetic version")
    (install / "RELEASE_MANIFEST.json").write_text("{}", encoding="utf-8")
    previous = _inventory(install)
    source = _package(tmp_path / "download", "3.50.0")
    if mutation == "changed":
        (source / "LiquidGlass.exe").write_bytes(b"Changed synthetic executable")
    elif mutation == "missing":
        (source / REQUIRED_RUNTIME[0]).unlink()
    elif mutation == "extra":
        (source / "unexpected.txt").write_text("Unlisted content", encoding="utf-8")
    else:
        manifest = json.loads((source / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
        manifest["target"]["bits"] = 32
        (source / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = _run_installer(source, install, local)
    assert result.returncode != 0
    assert _inventory(install) == previous
    assert not list(install.parent.glob("*.installing-*"))
    assert not list(install.parent.glob("*.previous-*"))


def test_real_installer_refuses_a_destination_inside_financial_data(tmp_path: Path) -> None:
    local = tmp_path / "local"
    install = local / "FinancasPessoais" / "dados" / "program"
    source = _package(tmp_path / "download", "3.50.0")
    result = _run_installer(source, install, local)
    assert result.returncode != 0
    assert not install.exists()


def test_real_helper_applies_real_installer_to_synthetic_package_without_launch(
    tmp_path: Path,
) -> None:
    local = tmp_path / "local"
    install = local / "Programs" / "FinancasPessoaisLiquidGlass"
    install.mkdir(parents=True)
    (install / "LiquidGlass.exe").write_bytes(b"Previous synthetic version")
    (install / "RELEASE_MANIFEST.json").write_text("{}", encoding="utf-8")
    data = local / "FinancasPessoais" / "dados"
    data.mkdir(parents=True)
    sentinel = data / "synthetic-finance.sqlite3"
    sentinel.write_bytes(b"Synthetic preserved data")
    work = tmp_path / "financas-update-synthetic ' & Unicode-ç"
    source = _package(work / "extracted" / "LiquidGlass", "3.50.0")
    (work / ".update-workdir").write_text("FinancasPessoais", encoding="ascii")
    helper = work / "APLICAR_ATUALIZACAO.ps1"
    helper.write_bytes((source / helper.name).read_bytes())
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(local)
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(helper), "-PackageRoot", str(source), "-InstallRoot", str(install),
         "-ExpectedVersion", "3.50.0", "-ParentProcessId", "2147483647",
         "-ExpectedManifestSha256", hashlib.sha256((source / "RELEASE_MANIFEST.json").read_bytes()).hexdigest(),
         "-ExpectedInstallerSha256", hashlib.sha256((source / "INSTALAR_FINANCAS_PESSOAIS.ps1").read_bytes()).hexdigest(),
         "-NoLaunch"],
        env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW, check=False,
    )
    assert result.returncode == 0, (result.stdout + result.stderr).decode(errors="replace")
    assert _inventory(install) == _inventory(source)
    assert sentinel.read_bytes() == b"Synthetic preserved data"
