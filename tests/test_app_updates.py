"""Synthetic update discovery, hostile archives and installation boundaries."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from services import app_updates as updates


def metadata(version="3.50.0", size=100):
    tag = f"v{version}"
    return {
        "tag_name": tag,
        "html_url": f"{updates.RELEASES_URL}/tag/{tag}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": name,
                "state": "uploaded",
                "size": size,
                "browser_download_url": f"https://github.com/{updates.REPOSITORY}/releases/download/{tag}/{name}",
            }
            for name in (
                f"LiquidGlass_App_v{version}.zip",
                f"LiquidGlass_App_v{version}.sha256.txt",
            )
        ],
    }


def package(extra=None, version="3.50.0"):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("LiquidGlass/LiquidGlass.exe", b"synthetic executable")
        archive.writestr(
            "LiquidGlass/INSTALAR_FINANCAS_PESSOAIS.ps1", "# synthetic installer"
        )
        archive.writestr(
            "LiquidGlass/RELEASE_MANIFEST.json", json.dumps({"version": version})
        )
        for name, data in (extra or {}).items():
            if isinstance(name, str) and "\\" in name:
                raw_name = zipfile.ZipInfo("temporary")
                raw_name.filename = name
                name = raw_name
            archive.writestr(name, data)
    return content.getvalue()


def fake_release(monkeypatch, data, *, digest=None, checksum=None):
    response = metadata(size=len(data))
    response["assets"][0]["digest"] = digest
    release = updates.release_from_metadata(response, "3.49.0")
    expected = checksum or hashlib.sha256(data).hexdigest()
    mapping = {
        release.asset_url: data,
        release.checksum_url: f"{expected}  {release.asset_name}\n".encode("ascii"),
    }
    monkeypatch.setattr(updates, "_open_url", lambda url: io.BytesIO(mapping[url]))
    return release


def test_stable_release_discovery():
    release = updates.release_from_metadata(metadata(), "3.49.0")
    assert release.version == "3.50.0"
    assert (
        release.html_url
        == "https://github.com/renafcampos27/Finan-as/releases/tag/v3.50.0"
    )
    assert updates.release_from_metadata(metadata(), "3.50.0") is None
    assert updates.release_from_metadata(metadata(), "4.0.0") is None
    assert updates.version_tuple("3.9.12") < updates.version_tuple("3.10.0")


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_ignore_unpublished_and_preview(flag):
    response = metadata()
    response[flag] = True
    assert updates.release_from_metadata(response, "3.49.0") is None


@pytest.mark.parametrize(
    "tag", ["3.50.0", "v3.50.0-rc.1", "v03.50.0", "v3.50", "v3.50.0/evil"]
)
def test_reject_ambiguous_versions(tag):
    response = metadata()
    response["tag_name"] = tag
    with pytest.raises(updates.UpdateError):
        updates.release_from_metadata(response, "3.49.0")


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_url",
        "missing_checksum",
        "duplicate_asset",
        "oversized",
        "bool_size",
        "bad_digest",
        "foreign_page",
    ],
)
def test_metadata_must_identify_official_complete_package(mutation):
    response = metadata()
    if mutation == "foreign_url":
        response["assets"][0]["browser_download_url"] = (
            "https://example.com/payload.zip"
        )
    elif mutation == "missing_checksum":
        response["assets"].pop()
    elif mutation == "duplicate_asset":
        response["assets"].append(response["assets"][0])
    elif mutation == "oversized":
        response["assets"][0]["size"] = updates.MAX_ARCHIVE_BYTES + 1
    elif mutation == "bool_size":
        response["assets"][0]["size"] = True
    elif mutation == "bad_digest":
        response["assets"][0]["digest"] = "md5:untrusted"
    else:
        response["html_url"] = "https://github.com/other/repo/releases/tag/v3.50.0"
    with pytest.raises(updates.UpdateError):
        updates.release_from_metadata(response, "3.49.0")


def test_empty_repository_and_network_failure(monkeypatch):
    def no_release(url):
        raise urllib.error.HTTPError(url, 404, "No release", {}, None)

    monkeypatch.setattr(updates, "_open_url", no_release)
    assert updates.check_for_update("3.49.0") is None
    monkeypatch.setattr(updates, "_open_url", lambda url: io.BytesIO(b"not json"))
    with pytest.raises(updates.UpdateError):
        updates.check_for_update("3.49.0")


@pytest.mark.parametrize(
    "target",
    [
        "http://github.com/file",
        "https://example.com/file",
        "https://github.com.evil.org/file",
        "https://user@github.com/file",
        "https://github.com:444/file",
    ],
)
def test_redirect_never_downgrades_or_leaves_github(target):
    handler = updates._SafeRedirect()
    with pytest.raises(updates.UpdateError):
        handler.redirect_request(
            urllib.request.Request("https://github.com/file"),
            None,
            302,
            "Found",
            {},
            target,
        )


def test_download_verifies_digest_and_extracts_separate_stage(monkeypatch, tmp_path):
    data = package()
    digest = hashlib.sha256(data).hexdigest()
    release = fake_release(monkeypatch, data, digest=f"sha256:{digest}")
    progress = []
    staged = updates.download_update(
        release, tmp_path, lambda got, total: progress.append((got, total))
    )
    assert staged.sha256 == digest
    assert staged.package_root.is_relative_to(staged.work_dir)
    assert (
        staged.package_root / "LiquidGlass.exe"
    ).read_bytes() == b"synthetic executable"
    assert progress[-1] == (len(data), len(data))


@pytest.mark.parametrize(
    "failure",
    [
        "truncated",
        "corrupt",
        "conflicting_digest",
        "wrong_version",
        "oversized_stream",
        "cancelled",
    ],
)
def test_failed_download_cleans_only_its_own_directory(monkeypatch, tmp_path, failure):
    data = package(version="3.49.0" if failure == "wrong_version" else "3.50.0")
    release = fake_release(
        monkeypatch, data, checksum="0" * 64 if failure == "corrupt" else None
    )
    if failure == "truncated":
        release = replace(release, asset_size=len(data) + 1)
    if failure == "oversized_stream":
        release = replace(release, asset_size=len(data) - 1)
    if failure == "conflicting_digest":
        release = replace(release, digest="0" * 64)
    cancel_event = threading.Event()
    if failure == "cancelled":
        cancel_event.set()
    preserved = tmp_path / "unrelated.txt"
    preserved.write_text("preserve me", encoding="utf-8")
    with pytest.raises(updates.UpdateError):
        updates.download_update(release, tmp_path, cancel_event=cancel_event)
    assert list(tmp_path.iterdir()) == [preserved]


@pytest.mark.parametrize(
    "name",
    [
        "../outside.txt",
        "/outside.txt",
        "LiquidGlass/../outside.txt",
        "LiquidGlass/dir\\outside.txt",
        "LiquidGlass/C:/outside.txt",
        "LiquidGlass/a:stream",
        "LiquidGlass/CON.txt",
        "LiquidGlass/COM¹.txt",
        "LiquidGlass/CONOUT$",
        "LiquidGlass/file. ",
        "LiquidGlass/Liquidglass.EXE",
        "OtherApp/file.txt",
        "LiquidGlass//file.txt",
    ],
)
def test_hostile_zip_paths_are_rejected_before_any_extraction(
    monkeypatch, tmp_path, name
):
    release = fake_release(monkeypatch, package({name: b"malicious"}))
    with pytest.raises(updates.UpdateError):
        updates.download_update(release, tmp_path)
    assert not list(tmp_path.iterdir())
    assert not (tmp_path.parent / "outside.txt").exists()


def test_symlink_member_is_rejected(monkeypatch, tmp_path):
    symlink = zipfile.ZipInfo("LiquidGlass/symlink")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    release = fake_release(monkeypatch, package({symlink: b"../../outside"}))
    with pytest.raises(updates.UpdateError):
        updates.download_update(release, tmp_path)
    assert not list(tmp_path.iterdir())


def test_extraction_limits(monkeypatch, tmp_path):
    release = fake_release(monkeypatch, package())
    monkeypatch.setattr(updates, "MAX_EXTRACTED_BYTES", 1)
    with pytest.raises(updates.UpdateError):
        updates.download_update(release, tmp_path)
    assert not list(tmp_path.iterdir())


def test_source_and_portable_build_cannot_replace_another_install(
    monkeypatch, tmp_path
):
    monkeypatch.delattr(updates.sys, "frozen", raising=False)
    assert updates.managed_install_root() is None
    monkeypatch.setattr(updates.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(
        updates.sys, "executable", str(tmp_path / "portable" / "LiquidGlass.exe")
    )
    assert updates.managed_install_root() is None
    standard = (
        Path(os.environ["LOCALAPPDATA"]) / "Programs" / "FinancasPessoaisLiquidGlass"
    )
    standard.mkdir(parents=True)
    (standard / "RELEASE_MANIFEST.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(updates.sys, "executable", str(standard / "LiquidGlass.exe"))
    assert updates.managed_install_root() == standard.resolve()


def test_launcher_keeps_paths_as_arguments_and_never_starts_source_install(
    monkeypatch, tmp_path
):
    release = fake_release(monkeypatch, package())
    prepared = updates.download_update(release, tmp_path / "cache")
    monkeypatch.setattr(updates, "managed_install_root", lambda: None)
    with pytest.raises(updates.UpdateError):
        updates.launch_windows_update(prepared)
    install = tmp_path / "installed with spaces ' & chars"
    install.mkdir()
    (install / updates.HELPER_NAME).write_text("# synthetic helper", encoding="utf-8")
    monkeypatch.setattr(updates, "managed_install_root", lambda: install)
    invocations = []
    monkeypatch.setattr(
        updates.subprocess,
        "Popen",
        lambda command, **kwargs: invocations.append((command, kwargs)),
    )
    monkeypatch.setattr(
        updates.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False
    )
    monkeypatch.setattr(
        updates.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False
    )
    updates.launch_windows_update(prepared, parent_pid=1234)
    command, kwargs = invocations[0]
    assert command[command.index("-InstallRoot") + 1] == str(install)
    assert command[command.index("-ParentProcessId") + 1] == "1234"
    assert command[command.index("-File") + 1] == str(
        prepared.work_dir / updates.HELPER_NAME
    )
    assert "shell" not in kwargs
    assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell helper")
@pytest.mark.parametrize(
    "scenario",
    [
        "valid",
        "wrong_target",
        "wrong_version",
        "tamper_installer",
        "tamper_manifest",
        "locked_files",
    ],
)
def test_powershell_handoff_on_synthetic_package_only(monkeypatch, tmp_path, scenario):
    """Exercise the real helper without invoking the real installer or program."""
    local_data = tmp_path / "local data"
    local_data.mkdir()
    work_root = tmp_path / "financas-update-package ' & Unicode-ç"
    source = work_root / "extracted/LiquidGlass"
    source.mkdir(parents=True)
    (work_root / ".update-workdir").write_text("FinancasPessoais", encoding="ascii")
    target = local_data / "Programs" / "FinancasPessoaisLiquidGlass"
    if scenario == "wrong_target":
        target = tmp_path / "unrelated installation"
    (source / "RELEASE_MANIFEST.json").write_text(
        json.dumps({"version": "3.49.0" if scenario == "wrong_version" else "3.50.0"}),
        encoding="utf-8",
    )
    marker = tmp_path / "fake-installer-ran.txt"
    lock_probe = (
        (
            "$lockDenied = $false\n"
            "try { [IO.File]::WriteAllText($PSCommandPath, 'tampered') } catch { $lockDenied = $true }\n"
            "if (-not $lockDenied) { exit 12 }\n"
        )
        if scenario == "locked_files"
        else ""
    )
    (source / "INSTALAR_FINANCAS_PESSOAIS.ps1").write_text(
        "param([string]$InstallRoot, [switch]$NoShortcuts, [switch]$NonInteractive)\n"
        + lock_probe
        + "[IO.File]::WriteAllText($env:FINANCAS_UPDATE_TEST_MARKER, $InstallRoot)\nexit 0\n",
        encoding="utf-8",
    )
    manifest_hash = hashlib.sha256(
        (source / "RELEASE_MANIFEST.json").read_bytes()
    ).hexdigest()
    installer_hash = hashlib.sha256(
        (source / "INSTALAR_FINANCAS_PESSOAIS.ps1").read_bytes()
    ).hexdigest()
    if scenario == "tamper_installer":
        with (source / "INSTALAR_FINANCAS_PESSOAIS.ps1").open(
            "a", encoding="utf-8"
        ) as output:
            output.write("# modified after download")
    elif scenario == "tamper_manifest":
        (source / "RELEASE_MANIFEST.json").write_text(
            '{"version":"3.50.0","modified":true}', encoding="utf-8"
        )
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))
    monkeypatch.setenv("FINANCAS_UPDATE_TEST_MARKER", str(marker))
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    helper_source = (
        Path(__file__).resolve().parents[1]
        / "packaging/windows/APLICAR_ATUALIZACAO.ps1"
    )
    helper = work_root / updates.HELPER_NAME
    shutil.copy2(helper_source, helper)
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(helper),
            "-PackageRoot",
            str(source),
            "-InstallRoot",
            str(target),
            "-ExpectedVersion",
            "3.50.0",
            "-ExpectedManifestSha256",
            manifest_hash,
            "-ExpectedInstallerSha256",
            installer_hash,
            "-ParentProcessId",
            "2147483647",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if scenario in ("valid", "locked_files"):
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert marker.read_text(encoding="utf-8") == str(target)
    else:
        assert result.returncode != 0
        assert not marker.exists()
    assert not target.exists()
