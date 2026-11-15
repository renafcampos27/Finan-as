#!/usr/bin/env python3
"""Run the dense real-Windows visual matrix at 100%, 125%, 150% and 200%."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "visual_stress",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if os.name != "nt":
        print("A matriz visual de stress requer o motor gráfico do Windows.")
        return 2
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    validator = PROJECT_ROOT / "scripts" / "validate_visual_rendering.py"
    for scale in (1.0, 1.25, 1.5, 2.0):
        scale_dir = output_dir / f"scale_{scale:g}"
        completed = subprocess.run(
            [
                sys.executable,
                str(validator),
                "--stress-only",
                "--scale-factor",
                f"{scale:g}",
                "--output-dir",
                str(scale_dir),
            ],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            sys.stderr.write(completed.stdout)
            sys.stderr.write(completed.stderr)
            return completed.returncode
        manifest_path = scale_dir / "visual_stress.json"
        if not manifest_path.exists():
            raise RuntimeError(f"Manifesto visual em falta: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        results.append(
            {
                "scale_factor": scale,
                "manifest": str(manifest_path.relative_to(output_dir)),
                "captures": len(manifest.get("captures") or []),
                "seeded_categories": int(manifest.get("seeded_categories") or 0),
                "detail_rows": int(
                    (manifest.get("chart_detail") or {}).get("rows") or 0
                ),
                "empty_charts": int(
                    (manifest.get("empty_states") or {}).get("charts") or 0
                ),
            }
        )

    aggregate = {
        "scales": results,
        "total_captures": sum(row["captures"] for row in results),
        "passed": all(
            row["captures"] == 4
            and row["seeded_categories"] >= 30
            and row["detail_rows"] >= 1
            and row["empty_charts"] >= 4
            for row in results
        ),
    }
    if not aggregate["passed"]:
        raise RuntimeError(f"Matriz visual incompleta: {aggregate}")
    target = output_dir / "visual_stress_matrix.json"
    target.write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(aggregate, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
