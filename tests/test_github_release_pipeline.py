"""Official releases retain provenance checks with CI output outside source."""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.release_gate import verify_release_gate


def _project(root: Path) -> Path:
    (root / "utils").mkdir(parents=True)
    (root / "utils/version.py").write_text(
        'APP_VERSION="1.2.3"\nAPP_VERSION_LABEL="v1.2.3"\nRELEASE_VERSION="V1.2"\n',
        encoding="utf-8",
    )
    for name in ("RELEASE_NOTES.md", "RELEASE_NOTES_RESUME.md"):
        (root / name).write_text("## V1.2 — v1.2.3 — synthetic release\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("absolute", [False, True])
def test_ci_output_directory_is_used_instead_of_historical_release(tmp_path, absolute):
    root = _project(tmp_path / "project")
    output = root / "artifacts/release"
    output.mkdir(parents=True)
    package = output / "LiquidGlass_App_v1.2.3.zip"
    manifest = {"version": "1.2.3", "installation": {"install_test": {"passed": True}}}
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("LiquidGlass/RELEASE_MANIFEST.json", json.dumps(manifest))
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    package.with_suffix(".sha256.txt").write_text(f"{digest}  {package.name}\n", encoding="ascii")
    directory = output if absolute else Path("artifacts/release")
    report = verify_release_gate(root, release_directory=directory)
    assert report["ok"], report
    assert report["candidate_package"] == package.name
    assert report["packages"][0]["sidecar_matched"] is True
    # Absence of a default release/ directory cannot silently select other bytes.
    assert verify_release_gate(root)["candidate_package"] is None


def test_strict_source_provenance_requires_a_git_checkout(tmp_path):
    root = _project(tmp_path / "project")
    report = verify_release_gate(root, strict_git=True)
    assert report["ok"] is False
    assert any("Git checkout is required" in error for error in report["errors"])


def test_strict_source_provenance_rejects_an_unreadable_git_checkout(tmp_path):
    root = _project(tmp_path / "project")
    (root / ".git").mkdir()
    report = verify_release_gate(root, strict_git=True)
    assert report["ok"] is False
    assert any("Git inspection failed" in error for error in report["errors"])


def test_strict_gate_rejects_a_release_tag_that_is_not_head(tmp_path):
    root = _project(tmp_path / "project")

    def git(*arguments):
        return subprocess.run(
            ["git", *arguments], cwd=root, capture_output=True, text=True,
            check=True, timeout=15,
        )

    git("init", "--quiet")
    git("config", "user.name", "Synthetic release test")
    git("config", "user.email", "release-test@example.invalid")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "initial")
    git("-c", "tag.gpgsign=false", "tag", "v1.2.3")
    (root / "later.txt").write_text("later source", encoding="utf-8")
    git("add", "later.txt")
    git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "later")
    report = verify_release_gate(root, strict=True)
    assert report["ok"] is False
    assert any("does not identify the current HEAD" in error for error in report["errors"])
