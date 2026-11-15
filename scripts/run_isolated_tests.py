"""Run pytest modules in separate processes, with retained logs and a summary."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SERIAL_MEASUREMENTS = frozenset({
    "test_startup_responsiveness.py",
    "test_runtime_resilience.py",
    "test_visual_rendering_windows.py",
})

def main() -> int:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "modules", nargs="*", help="Project-relative test modules or node IDs"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Directory for logs and summary.json"
    )
    parser.add_argument("--profile", choices=("empty", "demo"), default="empty")
    parser.add_argument("--jobs", type=int, choices=range(1, 5), default=1)
    parser.add_argument(
        "--serial-pattern", default="*benchmark*.py",
        help="Test-module filename glob to run alone after concurrent modules drain",
    )
    parser.add_argument(
        "--resume", action="store_true", help="Continue the saved module inventory"
    )
    args = parser.parse_args()
    modules = args.modules or [
        str(path.relative_to(project))
        for path in sorted((project / "tests").glob("*.py"))
        if path.name.startswith(("test_", "smoke_"))
    ]
    args.output.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        FINANCAS_PESSOAIS_TESTING="1",
        PYTHONDONTWRITEBYTECODE="1",
        QT_QPA_PLATFORM="offscreen",
        PYTHONUTF8="1",
    )
    summary = args.output / "summary.json"
    results = []
    if args.resume and summary.exists():
        saved = json.loads(summary.read_text(encoding="utf-8"))
        if saved["profile"] != args.profile:
            parser.error("Cannot resume a different profile")
        results = saved["results"]
    done = {result["module"] for result in results}

    def run(index: int, module: str) -> dict:
        started = time.monotonic()
        log = args.output / f"{index:03d}-{Path(module.split('::')[0]).stem}.log"
        command = [
            sys.executable,
            "-m",
            "pytest",
            module,
            "-q",
            "--tb=short",
            f"--finance-profile={args.profile}",
        ]
        with log.open("w", encoding="utf-8") as handle:
            completed = subprocess.run(
                command,
                cwd=project,
                env=environment,
                stdout=handle,
                stderr=subprocess.STDOUT,
                check=False,
            )
        result = {
            "module": module,
            "returncode": completed.returncode,
            "status": "passed"
            if completed.returncode == 0
            else ("no_tests" if completed.returncode == 5 else "failed"),
            "seconds": round(time.monotonic() - started, 2),
            "log": str(log.resolve()),
        }
        return result

    def record(result: dict) -> None:
        results.append(result)
        summary.write_text(
            json.dumps({"profile": args.profile, "results": results}, indent=2),
            encoding="utf-8",
        )
        print(
            f"[{len(results)}/{len(modules)}] {result['module']}: exit {result['returncode']} ({result['seconds']}s)",
            flush=True,
        )

    concurrent_modules = []
    serial_modules = []
    for index, module in enumerate(modules, 1):
        if module in done:
            continue
        filename = Path(module.split("::")[0]).name
        needs_serial = filename in SERIAL_MEASUREMENTS or fnmatch.fnmatch(
            filename, args.serial_pattern
        )
        target = serial_modules if needs_serial else concurrent_modules
        target.append((index, module))
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run, index, module) for index, module in concurrent_modules]
        for future in as_completed(futures):
            record(future.result())
    # Wall-clock performance checks must not compete with other test modules.
    # They remain mandatory and retain their original acceptance thresholds.
    for index, module in serial_modules:
        record(run(index, module))
    # Some legacy smoke files expose only main(); smoke_script_execution.py
    # exercises those entry points. An empty pytest module is not a pass.
    return int(any(result["returncode"] not in (0, 5) for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
