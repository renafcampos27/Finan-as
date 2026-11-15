from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.prepare_github_source import (
    MAX_SOURCE_FILE_BYTES,
    REQUIRED_FILES,
    SourceExportError,
    collect_source,
    prepare_source,
)


def _minimal_source(root: Path) -> Path:
    root.mkdir()
    for name in REQUIRED_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# synthetic source\n", encoding="utf-8")
    return root


def _write(root: Path, name: str, data: str) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(data, encoding="utf-8")


def test_export_uses_allowlist_and_keeps_manifest_outside_git_source(tmp_path: Path) -> None:
    source = _minimal_source(tmp_path / "project")
    allowed = {
        "motores_matematicos_simulacao/model.py": "# synthetic math\n",
        "packaging/windows/INSTALL.ps1": "# synthetic install\n",
        ".github/workflows/test.yml": "name: synthetic\n",
        "tests/test_example.py": "# synthetic fixture\n",
        "assets/style.qss": "QWidget {}\n",
        "README.md": "Synthetic public documentation\n",
    }
    for name, data in allowed.items():
        _write(source, name, data)
    excluded = [
        ".git/config", ".env", "financas.sqlite3", "logs/app.log",
        "backups/profile.lgbak", "sessaoopencode/transcript.txt",
        "sessaoopencode - Cópia chatgpt/plan.md", "_scratch/debug.py",
        "utils/backup_preferences.json", "utils/private.sqlite3",
        "utils/__pycache__/cached.py", "tests/data/personal.csv",
        "scripts/.agents/private.py", "STATUS.md",
    ]
    for name in excluded:
        _write(source, name, "synthetic private value\n")
    output = tmp_path / "export"
    report = prepare_source(source, output)
    actual = {
        path.relative_to(output / "source").as_posix()
        for path in (output / "source").rglob("*") if path.is_file()
    }
    assert actual == REQUIRED_FILES | allowed.keys()
    assert not any((output / "source" / name).exists() for name in excluded)
    assert not (output / "source" / "manifest.json").exists()
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == report
    for item in report["files"]:
        data = (output / "source" / item["path"]).read_bytes()
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
        assert item["size"] == len(data)


@pytest.mark.parametrize("credential", [
    "ghp_" + "a" * 36,
    "github_pat_" + "b" * 40,
    "sk-" + "c" * 40,
    "AIza" + "d" * 35,
    "AKIA" + "E" * 16,
    "-----BEGIN " + "PRIVATE KEY-----",
])
def test_credential_scan_blocks_before_creating_destination_and_redacts_value(
    tmp_path: Path, credential: str,
) -> None:
    source = _minimal_source(tmp_path / "project")
    _write(source, "services/transport.py", "# configuration\nTOKEN = " + repr(credential))
    output = tmp_path / "export"
    with pytest.raises(SourceExportError) as error:
        prepare_source(source, output)
    assert "services/transport.py:2" in str(error.value)
    assert credential not in str(error.value)
    assert not output.exists()


def test_test_directory_does_not_bypass_secret_scan(tmp_path: Path) -> None:
    source = _minimal_source(tmp_path / "project")
    _write(source, "tests/test_audit_d4_d5_e1_e2_defects.py", "key = " + repr("AIza" + "x" * 35))
    with pytest.raises(SourceExportError, match="Possible Google token"):
        collect_source(source)


def test_existing_destination_is_preserved(tmp_path: Path) -> None:
    source = _minimal_source(tmp_path / "project")
    output = tmp_path / "export"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(SourceExportError, match="already exists"):
        prepare_source(source, output)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_in_source_destination_is_restricted_to_excluded_artifacts(tmp_path: Path) -> None:
    source = _minimal_source(tmp_path / "project")
    with pytest.raises(SourceExportError, match="Destination must be outside"):
        prepare_source(source, source / "services" / "export")
    output = source / "artifacts" / "github-export"
    report = prepare_source(source, output)
    assert len(report["files"]) == len(REQUIRED_FILES)


@pytest.mark.parametrize("directory", [False, True])
def test_selected_source_links_are_rejected(tmp_path: Path, directory: bool) -> None:
    source = _minimal_source(tmp_path / "project")
    target = tmp_path / ("external-dir" if directory else "external.py")
    if directory:
        target.mkdir()
        _write(target, "private.py", "# private")
    else:
        target.write_text("# private", encoding="utf-8")
    link = source / "services" / ("linked" if directory else "linked.py")
    link.parent.mkdir()
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("Symlink creation is unavailable for this Windows account")
    with pytest.raises(SourceExportError, match="links/reparse"):
        collect_source(source)


def test_required_source_and_text_encoding_are_checked(tmp_path: Path) -> None:
    source = _minimal_source(tmp_path / "project")
    (source / "main.py").unlink()
    with pytest.raises(SourceExportError, match="Required source missing: main.py"):
        collect_source(source)
    (source / "main.py").write_bytes(b"\xff\xfe")
    with pytest.raises(SourceExportError, match="Non-UTF-8 source: main.py"):
        collect_source(source)


def test_windows_reparse_point_is_rejected_without_symlink_privileges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _minimal_source(tmp_path / "project")
    selected = source / "main.py"
    original = Path.lstat

    def metadata(path: Path):
        result = original(path)
        if path == selected:
            return SimpleNamespace(st_file_attributes=0x400, st_mode=result.st_mode)
        return result

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(SourceExportError, match="links/reparse"):
        collect_source(source)


def test_oversized_source_fails_before_creating_destination(tmp_path: Path) -> None:
    source = _minimal_source(tmp_path / "project")
    _write(source, "services/large.py", "#" * (MAX_SOURCE_FILE_BYTES + 1))
    output = tmp_path / "export"
    with pytest.raises(SourceExportError, match="exceeds review limit"):
        prepare_source(source, output)
    assert not output.exists()
