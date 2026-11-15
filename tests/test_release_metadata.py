from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from utils.helpers import APP_TITLE
from utils.version import (
    APP_VERSION,
    APP_VERSION_LABEL,
    RELEASE_DATE,
    RELEASE_VERSION,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _release_headings(path: Path) -> list[tuple[str, str]]:
    return re.findall(
        r"^## (V\d+\.\d+) — v(\d+\.\d+\.\d+) — ",
        path.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )


def test_v3_release_metadata_has_one_source_of_truth() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION)
    assert APP_VERSION_LABEL == f"v{APP_VERSION}"
    assert RELEASE_VERSION == "V" + ".".join(APP_VERSION.split(".")[:2])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", RELEASE_DATE)
    assert APP_VERSION_LABEL in APP_TITLE


def test_current_release_is_first_in_both_permanent_histories() -> None:
    technical = PROJECT_ROOT / "RELEASE_NOTES.md"
    summary = PROJECT_ROOT / "RELEASE_NOTES_RESUME.md"

    for path in (technical, summary):
        assert path.is_file()
        headings = _release_headings(path)
        assert headings
        assert headings[0] == (RELEASE_VERSION, APP_VERSION)
        assert len(headings) == len(set(headings))


def test_windows_package_workflow_includes_both_release_files() -> None:
    build_script = (PROJECT_ROOT / "scripts" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )

    assert "RELEASE_NOTES.md" in build_script
    assert "RELEASE_NOTES_RESUME.md" in build_script
    assert "DATA_SAFETY.md" in build_script


def test_windows_package_includes_every_lazily_loaded_page_module() -> None:
    spec = (PROJECT_ROOT / "LiquidGlass.spec").read_text(encoding="utf-8")

    assert 'collect_submodules("ui.pages")' in spec
    assert "hiddenimports=page_hidden_imports" in spec


def test_windows_package_records_and_guards_source_provenance() -> None:
    script = (PROJECT_ROOT / "scripts" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )
    assert "& $gitExecutable rev-parse --verify HEAD" in script
    assert "refs/tags/$sourceTag^{commit}" in script
    assert "& $gitExecutable status --porcelain --untracked-files=normal" in script
    assert script.count("\nAssert-SourceSnapshot\n") == 2
    for field in ("commit", "tree", "tag"):
        assert f"{field} = [string]$source" in script
    assert "dirty = $false" in script
    assert "artifacts\\package-recovery\\" in script
    assert 'Remove-GeneratedPath "LiquidGlass_App.zip"' not in script
    assert (
        'Move-Item -LiteralPath "$projectRoot\\build\\LiquidGlass_App_v$appVersion.zip"'
        in script
    )


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None, reason="Windows build guard"
)
@pytest.mark.parametrize(
    "state", ["clean", "clean-trimmed-path", "untracked", "modified", "missing-tag", "wrong-tag"]
)
def test_build_source_guard_rejects_unreleased_changes(
    tmp_path: Path, state: str
) -> None:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, timeout=15
        )

    git("init", "--quiet")
    git("config", "user.name", "Release test")
    git("config", "user.email", "release-test@example.invalid")
    source = tmp_path / "source.txt"
    source.write_text("committed source", encoding="utf-8")
    git("add", "source.txt")
    git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test source")
    if state != "missing-tag":
        git("-c", "tag.gpgsign=false", "tag", f"v{APP_VERSION}")
    if state == "untracked":
        (tmp_path / "new.txt").write_text("uncommitted", encoding="utf-8")
    if state in {"modified", "wrong-tag"}:
        source.write_text("different source", encoding="utf-8")
    if state == "wrong-tag":
        git("add", "source.txt")
        git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "later source")
    script = (PROJECT_ROOT / "scripts" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )
    guard = script.split("# Package only a committed", 1)[1].split(
        "# Retain existing distribution", 1
    )[0]
    guard = "# Package only a committed" + guard
    native_path_prefix = ""
    if state == "clean-trimmed-path":
        native_path_prefix = (
            "$gitExecutable = (Get-Command git.exe -CommandType Application | Select-Object -First 1).Source; "
            "$env:PATH = (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot;\n"
        )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f'$ErrorActionPreference = "Stop"; $appVersion = "{APP_VERSION}";\n'
            + native_path_prefix + guard,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) == (state in {"clean", "clean-trimmed-path"}), result.stderr
