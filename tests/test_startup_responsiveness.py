from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_startup_keeps_the_event_loop_responsive(tmp_path: Path) -> None:
    """Measure a clean launch without reusing pytest's long-lived Qt process."""
    project_root = Path(__file__).resolve().parents[1]
    probe = project_root / "scripts" / "probe_startup_responsiveness.py"
    env = os.environ.copy()
    env["FINANCAS_PESSOAIS_STATE_DIR"] = str(tmp_path / "profile")
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"

    completed = subprocess.run(
        [sys.executable, str(probe)],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"Startup probe failed.\nSTDOUT:\n{completed.stdout}\n"
        f"STDERR:\n{completed.stderr}"
    )

    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert output_lines, f"Startup probe produced no output: {completed.stderr}"
    metrics = json.loads(output_lines[-1])

    assert metrics["import_seconds"] < 5.0
    assert metrics["constructor_seconds"] < 5.0
    assert metrics["observation_seconds"] < 5.0
    # Cold Windows/Qt font and paint caches can coalesce the first timer
    # callbacks even though the window remains responsive.  Measure the user-
    # visible failure mode directly: the loop must answer repeatedly and may
    # not disappear for 800 ms in this 900 ms observation window.
    assert metrics["event_loop_pulses"] >= 4
    assert metrics["max_event_loop_gap_seconds"] < 0.8
    assert metrics["dashboard_resize_calls"] < 30
    assert metrics["budget_sync_calls"] < 30
    assert metrics["content_sync_calls"] <= 50
    assert metrics["dashboard_height"] < 10_000

    assert metrics["stack_height"] < 10_000
