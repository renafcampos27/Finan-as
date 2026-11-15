"""Council A01 / QA-05/08/14/15: synthetic profiles and write boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "operation",
    [
        "outside.write_text('changed')",
        "outside.open('ab').close()",
        "shutil.copyfile(source, outside)",
        "outside.unlink()",
        "outside.rename(root / 'stolen')",
        "source.replace(outside)",
        "outside.chmod(0o600)",
        "(outside.parent / 'unexpected').mkdir()",
        "sqlite3.connect(outside)",
        "sqlite3.connect(outside.as_uri() + '?mode=ro', uri=True)",
    ],
)
def test_child_process_blocks_and_records_outside_mutations(tmp_path, operation):
    root = tmp_path / "child-owned"
    root.mkdir()
    outside = tmp_path / "personal-sentinel"
    outside.write_text("unchanged", encoding="utf-8")
    source = root / "source"
    source.write_text("synthetic", encoding="utf-8")
    code = (
        "import shutil, sqlite3\nfrom pathlib import Path\n"
        f"root = Path({str(root)!r})\n"
        f"outside = Path({str(outside)!r})\n"
        "source = root / 'source'\n"
        "try:\n"
        f"    {operation}\n"
        "except PermissionError:\n    pass\n"
        "else:\n    raise AssertionError('outside operation was allowed')\n"
    )
    environment = os.environ.copy()
    environment["FINANCAS_PESSOAIS_TEST_ROOT"] = str(root)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert outside.read_text(encoding="utf-8") == "unchanged"
    assert source.read_text(encoding="utf-8") == "synthetic"
    violations = (
        (root / "isolation-violations.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert len(violations) == 1
    assert not (outside.parent / "unexpected").exists()


def test_home_temp_and_default_paths_are_session_owned():
    from utils.config import paths_for_database

    # Windows runners may expose the same directory through an 8.3 alias.
    # Resolve both sides so the containment check still follows real paths.
    root = Path(os.environ["FINANCAS_PESSOAIS_TEST_ROOT"]).resolve()
    assert Path.home().resolve().is_relative_to(root)
    for variable in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP"):
        assert Path(os.environ[variable]).resolve().is_relative_to(root)
    paths = paths_for_database()
    for path in (
        paths.database,
        paths.attachments,
        paths.backups,
        paths.configuration,
        paths.logs,
    ):
        assert path.resolve().is_relative_to(root)


def test_test_bootstrap_discards_inherited_environment_api_key():
    environment = os.environ.copy()
    environment["GEMINI_API_KEY"] = "synthetic-parent-key-must-not-leak"
    conftest = Path(__file__).with_name("conftest.py")
    code = (
        "import os, runpy\n"
        f"runpy.run_path({str(conftest)!r})\n"
        "assert 'GEMINI_API_KEY' not in os.environ\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("selection,expected", [(None, "0"), ("0", "0"), ("1", "1")])
def test_profile_defaults_to_empty_and_respects_explicit_worker_opt_in(
    selection, expected
):
    environment = os.environ.copy()
    environment.pop("FINANCAS_PESSOAIS_DEMO_DATA", None)
    if selection is not None:
        environment["FINANCAS_PESSOAIS_DEMO_DATA"] = selection
    conftest = Path(__file__).with_name("conftest.py")
    code = (
        "import os, runpy\n"
        f"runpy.run_path({str(conftest)!r})\n"
        "print(os.environ['FINANCAS_PESSOAIS_DEMO_DATA'])\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected


@pytest.mark.parametrize("profile,expected_empty", [("empty", True), ("demo", False)])
def test_explicit_profile_fixtures_create_the_expected_data(
    request, profile, expected_empty
):
    from database.engine import FinanceDB

    root = request.getfixturevalue(f"{profile}_profile")
    db = FinanceDB(root / "synthetic.sqlite3")
    try:
        assert (db.scalar("SELECT COUNT(*) FROM transactions") == 0) is expected_empty
        assert db.path.is_relative_to(root)
        assert db.scalar("PRAGMA foreign_keys") == 1
        assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        db.conn.close()


def test_caught_violation_still_fails_pytest_session(tmp_path):
    project = Path(__file__).resolve().parents[1]
    probe = tmp_path / "test_swallowed.py"
    outside = tmp_path / "must-not-exist"
    probe.write_text(
        "from pathlib import Path\n"
        "def test_swallowed():\n"
        "    try:\n"
        f"        Path({str(outside)!r}).write_text('private')\n"
        "    except PermissionError:\n        pass\n",
        encoding="utf-8",
    )
    code = (
        "import importlib.util, pytest\n"
        f"spec = importlib.util.spec_from_file_location('isolation_plugin', {str(project / 'tests' / 'conftest.py')!r})\n"
        "plugin = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(plugin)\n"
        f"result = pytest.main([{str(probe)!r}, '-c', {str(project / 'pytest.ini')!r}, '--rootdir', {str(tmp_path)!r}, '--confcutdir', {str(tmp_path)!r}, '-q'], plugins=[plugin])\n"
        "raise SystemExit(int(result))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "Test isolation: 1 blocked operations" in completed.stdout
    assert not outside.exists()


def test_write_sentinel_resolves_junction_or_symlink_escape(tmp_path):
    # Run the audit callback directly so this expected refusal is not counted
    # as an accidental mutation by the parent session.
    from scripts.test_sandbox import TestIsolationError, WriteSentinel

    root = tmp_path / "owned"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege is unavailable")
    sentinel = WriteSentinel(root)
    with pytest.raises(TestIsolationError):
        sentinel.audit("open", (link / "secret", "w", os.O_WRONLY))
    assert not (outside / "secret").exists()
    record = json.loads(sentinel.journal.read_text(encoding="utf-8"))
    assert Path(record["path"]) == outside / "secret"


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path namespace")
def test_extended_windows_paths_share_the_same_write_boundary(tmp_path):
    from scripts.test_sandbox import TestIsolationError, WriteSentinel

    root = tmp_path / "owned-namespace"
    root.mkdir()
    sentinel = WriteSentinel(root)
    source = root / "source.txt"
    source.write_text("synthetic", encoding="utf-8")
    target = root / "target.txt"
    extended_source = "\\\\?\\" + str(source.resolve())
    extended_target = "\\\\?\\" + str(target.resolve())
    sentinel.audit("os.rename", (extended_source, extended_target))
    os.replace(extended_source, extended_target)
    assert target.read_text(encoding="utf-8") == "synthetic"
    assert not sentinel.journal.exists()

    outside = tmp_path / "outside-namespace.txt"
    extended_outside = "\\\\?\\" + str(outside.resolve())
    with pytest.raises(TestIsolationError):
        sentinel.audit("open", (extended_outside, "w", os.O_WRONLY))
    assert not outside.exists()
    record = json.loads(sentinel.journal.read_text(encoding="utf-8"))
    assert Path(record["path"]) == outside
    with pytest.raises(TestIsolationError):
        sentinel.audit(
            "open", ("\\\\?\\" + str(root / ".." / outside.name), "w", os.O_WRONLY)
        )
    assert not outside.exists()
