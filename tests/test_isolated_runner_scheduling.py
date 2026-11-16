"""Performance modules run after functional workers, without being omitted."""

from __future__ import annotations

import json
import threading
import os
import sys
import ctypes
import time
from pathlib import Path

import pytest

from scripts import run_isolated_tests


def test_benchmarks_wait_for_functional_workers_even_after_failure(tmp_path, monkeypatch):
    modules = [
        "tests/test_benchmark_synthetic.py::test_measurement",
        "tests/test_startup_responsiveness.py",
        "tests/test_runtime_resilience.py",
        "tests/test_visual_rendering_windows.py",
        "tests/test_alpha.py",
        "tests/test_beta.py",
    ]
    output = tmp_path / "evidence"
    monkeypatch.setattr(
        run_isolated_tests.sys, "argv",
        ["run_isolated_tests.py", *modules, "--output", str(output), "--jobs", "2"],
    )
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    completed = []
    active = set()

    def fake_run(command, **kwargs):
        module = command[3]
        assert kwargs["env"]["FINANCAS_PESSOAIS_TESTING"] == "1"
        with lock:
            active.add(module)
        if module in modules[:4]:
            with lock:
                assert active == {module}, "Benchmark overlapped another test process"
                assert set(modules[4:]).issubset(completed)
        else:
            # Both functional processes must start before either completes.
            barrier.wait(timeout=10)
        with lock:
            active.remove(module)
            completed.append(module)
        return 1 if module.endswith("test_beta.py") else 0

    monkeypatch.setattr(run_isolated_tests, "run_process", fake_run)
    assert run_isolated_tests.main() == 1
    assert completed[-4:] == modules[:4]
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert {result["module"] for result in summary["results"]} == set(modules)
    assert [result["status"] for result in summary["results"]].count("failed") == 1


def test_process_capture_preserves_success(tmp_path):
    log = tmp_path / "success.log"
    with log.open("w", encoding="utf-8") as handle:
        result = run_isolated_tests.run_process(
            [sys.executable, "-c", "print('synthetic child completed')"],
            cwd=tmp_path, env=os.environ.copy(), output=handle, timeout=10,
        )
    assert result == 0
    assert "synthetic child completed" in log.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows owned-process-tree acceptance")
def test_process_timeout_terminates_parent_and_descendant(tmp_path):
    child_pid = tmp_path / "child.pid"
    command = (
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
        "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(120)"
    )
    log = tmp_path / "timeout.log"
    with log.open("w", encoding="utf-8") as handle:
        result = run_isolated_tests.run_process(
            [sys.executable, "-c", command, str(child_pid)],
            cwd=tmp_path, env=os.environ.copy(), output=handle, timeout=5,
        )
    assert result == 124
    assert "MODULE TIMEOUT" in log.read_text(encoding="utf-8")
    descendant = int(child_pid.read_text())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.OpenProcess(0x100000, False, descendant)
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 1000) == 0
        finally:
            kernel.CloseHandle(handle)


def test_watchdog_stack_survives_pytest_capture(tmp_path):
    if os.environ.get("FINANCAS_NATIVE_WATCHDOG_PROBE") == "1":
        time.sleep(120)
        return
    environment = os.environ.copy()
    environment["FINANCAS_NATIVE_WATCHDOG_PROBE"] = "1"
    environment["FINANCAS_PESSOAIS_TEST_WATCHDOG_SECONDS"] = "1"
    stacks = tmp_path / "captured.stacks.log"
    environment["FINANCAS_PESSOAIS_TEST_WATCHDOG_LOG"] = str(stacks)
    node = f"{Path(__file__).resolve()}::test_watchdog_stack_survives_pytest_capture"
    with (tmp_path / "child.log").open("w", encoding="utf-8") as handle:
        result = run_isolated_tests.run_process(
            [sys.executable, "-m", "pytest", node, "-q", "-p", "no:cacheprovider"],
            cwd=Path(__file__).resolve().parents[1], env=environment, output=handle,
            timeout=5,
        )
    assert result == 124
    trace = stacks.read_text(encoding="utf-8")
    assert "Timeout (" in trace
    assert "test_watchdog_stack_survives_pytest_capture" in trace
