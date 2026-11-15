from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_runtime_probe(tmp_path: Path, probe_name: str) -> dict[str, object]:
    project_root = Path(__file__).resolve().parents[1]
    probe = project_root / "scripts" / "probe_runtime_resilience.py"
    env = os.environ.copy()
    env["FINANCAS_PESSOAIS_STATE_DIR"] = str(tmp_path / "profile")
    env["FINANCAS_PESSOAIS_TESTING"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, str(probe), probe_name],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"Runtime probe failed.\nSTDOUT:\n{completed.stdout}\n"
        f"STDERR:\n{completed.stderr}"
    )
    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert output_lines, f"Runtime probe produced no output: {completed.stderr}"
    return json.loads(output_lines[-1])


def test_pytest_state_and_logs_are_outside_the_real_user_profile() -> None:
    from utils.app_logging import get_log_path
    from utils.config import app_state_root

    isolated_root = Path(os.environ["FINANCAS_PESSOAIS_STATE_DIR"]).resolve()
    assert os.environ.get("FINANCAS_PESSOAIS_TESTING") == "1"
    assert app_state_root() == isolated_root
    assert get_log_path().resolve().is_relative_to(isolated_root)


def test_subscription_reminder_timer_cannot_fire_after_close(
    tmp_path,
) -> None:
    metrics = _run_runtime_probe(tmp_path, "timer")
    assert metrics["fired_count"] == 0
    assert metrics["timer_active"] is False
    assert metrics["closing"] is True


def test_scheduled_backup_work_does_not_block_qt_events(
    tmp_path,
) -> None:
    metrics = _run_runtime_probe(tmp_path, "background-backup")
    assert metrics["task_started"] is True
    assert metrics["event_loop_pulses"] >= 8
    assert metrics["task_running_after_wait"] is False


def test_thread_owned_scheduled_backup_creates_a_verified_bundle(
    tmp_path,
    monkeypatch,
) -> None:
    import database.repo.base as repo_base

    database_path = tmp_path / "runtime.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database_path)
    from database.repo import Repo

    repo = Repo()
    repo.set_setting("auto_backup_interval_minutes", "1")
    repo.set_setting("last_auto_backup_dt", "")
    try:
        from services.backup import BackupService

        target = BackupService.run_scheduled_backup_for_database(
            database_path,
            silent=False,
        )
        assert target is not None
        assert target.is_file()
        assert target.parent == tmp_path / "backups"
        assert BackupService.validate_complete_archive(target)
    finally:
        repo.db.conn.close()
