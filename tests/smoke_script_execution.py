"""Execute every legacy smoke entry point offline, not just its test_* functions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPTS = (
    "smoke_ai_service.py",
    "smoke_analytics_service.py",
    "smoke_async_task.py",
    "smoke_backup_service.py",
    "smoke_document_service.py",
    "smoke_gemini_transport.py",
    "smoke_import_service.py",
    "smoke_payments_service.py",
    "smoke_repo.py",
    "smoke_secret_store.py",
)

# Install the guard before importing application code. Record attempted network
# access so even a script that catches the exception must fail validation.
OFFLINE_BOOTSTRAP = """
import sys

blocked = []
def deny_network(event, args):
    if event in {
        'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
        'socket.gethostbyaddr', 'socket.sendto', 'socket.sendmsg',
    }:
        blocked.append(event)
        raise RuntimeError('SMOKE_NETWORK_FORBIDDEN: ' + event)

sys.addaudithook(deny_network)
try:
    exec(compile(sys.argv[1], '<offline-smoke>', 'exec'), {'__name__': '__main__'})
finally:
    if blocked:
        raise AssertionError('SMOKE_NETWORK_FORBIDDEN: ' + ', '.join(blocked))
"""


def run_offline(
    program: str, state_root: Path, *, timeout: float = 30
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("PYTHONOPTIMIZE", None)
    env.update(
        FINANCAS_PESSOAIS_STATE_DIR=str(state_root),
        FINANCAS_PESSOAIS_TESTING="1",
        FINANCAS_PESSOAIS_DEMO_DATA="1",
        QT_QPA_PLATFORM="offscreen",
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return subprocess.run(
        [sys.executable, "-c", OFFLINE_BOOTSTRAP, program],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def test_smoke_entrypoint_inventory_is_complete() -> None:
    scripts = {path.name for path in (ROOT / "tests").glob("smoke_*.py")}
    scripts.remove(Path(__file__).name)
    assert scripts == set(SMOKE_SCRIPTS), "Add every new smoke script to the runner"


@pytest.mark.parametrize("script", SMOKE_SCRIPTS)
def test_smoke_script_entrypoint(script: str, tmp_path: Path) -> None:
    path = ROOT / "tests" / script
    result = run_offline(
        f"import runpy; runpy.run_path({str(path)!r}, run_name='__main__')",
        tmp_path / "state",
    )
    assert result.returncode == 0, (
        f"{script} failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout, result.stdout


@pytest.mark.parametrize(
    "operation",
    [
        "socket.getaddrinfo('example.invalid', 443)",
        "socket.socket().connect(('192.0.2.1', 443))",
        "socket.socket().connect_ex(('192.0.2.1', 443))",
        "socket.socket(type=socket.SOCK_DGRAM).sendto(b'x', ('192.0.2.1', 443))",
    ],
)
def test_smoke_network_guard_rejects_swallowed_attempts(
    operation: str, tmp_path: Path
) -> None:
    program = f"import socket\ntry:\n    {operation}\nexcept Exception:\n    pass\n"
    result = run_offline(program, tmp_path / "state")
    assert result.returncode != 0
    assert "SMOKE_NETWORK_FORBIDDEN" in result.stderr


def test_smoke_runner_preserves_assertion_failures(tmp_path: Path) -> None:
    result = run_offline("assert False, 'smoke assertion sentinel'", tmp_path / "state")
    assert result.returncode != 0
    assert "AssertionError: smoke assertion sentinel" in result.stderr


def test_smoke_runner_enforces_deadline(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run_offline("import time; time.sleep(10)", tmp_path / "state", timeout=0.5)
