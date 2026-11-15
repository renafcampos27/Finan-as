"""Performance modules run after functional workers, without being omitted."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

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
        return SimpleNamespace(returncode=1 if module.endswith("test_beta.py") else 0)

    monkeypatch.setattr(run_isolated_tests.subprocess, "run", fake_run)
    assert run_isolated_tests.main() == 1
    assert completed[-4:] == modules[:4]
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert {result["module"] for result in summary["results"]} == set(modules)
    assert [result["status"] for result in summary["results"]].count("failed") == 1
