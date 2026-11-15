"""A02: foreign PDF/image runtime DLLs must not contaminate the bundle."""

from __future__ import annotations

import os

import pytest

from scripts.bundle_provenance import (
    trusted_build_path,
    trusted_runtime_path,
    validate_binary_provenance,
)


def _roots(tmp_path):
    roots = {
        "source_root": tmp_path / "snapshot",
        "environment_root": tmp_path / "build-env",
        "base_python_root": tmp_path / "base-python",
        "windows_root": tmp_path / "Windows",
    }
    for root in roots.values():
        root.mkdir()
    return roots


def test_native_search_paths_discard_inherited_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "codex-runtimes/native/poppler/bin"))
    windows, base, executable = (
        tmp_path / "Windows",
        tmp_path / "python",
        tmp_path / "env/Scripts/python.exe",
    )
    build = trusted_build_path(executable, base, windows).split(os.pathsep)
    runtime = trusted_runtime_path(windows).split(os.pathsep)
    assert build == [
        str(p.resolve())
        for p in (executable.parent, base, base / "DLLs", windows / "System32", windows)
    ]
    assert runtime == [str(p.resolve()) for p in (windows / "System32", windows)]
    assert all("poppler" not in path for path in build + runtime)


@pytest.mark.parametrize(
    "scope", ["source_root", "environment_root", "base_python_root", "windows_root"]
)
def test_selected_source_environment_and_os_binary_origins_are_recorded(
    tmp_path, scope
):
    roots = _roots(tmp_path)
    library = roots[scope] / "library.dll"
    library.write_bytes(b"synthetic PE origin fixture")
    result = validate_binary_provenance(
        [("library.dll", str(library), "BINARY")], **roots
    )
    assert len(result) == 1
    assert result[0]["source"] == str(library.resolve())
    assert len(result[0]["sha256"]) == 64


def test_foreign_native_runtime_is_rejected_before_collect(tmp_path):
    roots = _roots(tmp_path)
    foreign = tmp_path / "codex-runtimes/native/poppler/icuuc.dll"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"exports ucnv_open_78 instead of required ucnv_open")
    with pytest.raises(ValueError, match="outside the selected runtime"):
        validate_binary_provenance([("icuuc.dll", str(foreign), "BINARY")], **roots)


def test_host_site_packages_is_rejected_even_below_base_python(tmp_path):
    roots = _roots(tmp_path)
    host = roots["base_python_root"] / "Lib/site-packages/foreign/library.pyd"
    host.parent.mkdir(parents=True)
    host.write_bytes(b"global package, not the isolated venv")
    with pytest.raises(ValueError, match="host site-packages"):
        validate_binary_provenance([("library.pyd", str(host), "EXTENSION")], **roots)
