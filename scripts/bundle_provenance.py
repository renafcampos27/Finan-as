"""Constrain native DLL discovery to the selected build environment and OS."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable


def _joined_paths(paths: Iterable[Path]) -> str:
    unique: dict[str, str] = {}
    for path in paths:
        value = str(path.resolve())
        unique.setdefault(value.casefold(), value)
    return os.pathsep.join(unique.values())


def trusted_build_path(executable: Path, base_python: Path, windows: Path) -> str:
    """Do not inherit caller PATH entries such as PDF/image tool runtimes."""
    return _joined_paths(
        (
            executable.parent,
            base_python,
            base_python / "DLLs",
            windows / "System32",
            windows,
        )
    )


def trusted_runtime_path(windows: Path) -> str:
    """The frozen loader supplies its own bundle; it needs no host Python."""
    return _joined_paths((windows / "System32", windows))


def validate_binary_provenance(
    binaries: Iterable[tuple[str, str, str]],
    *,
    source_root: Path,
    environment_root: Path,
    base_python_root: Path,
    windows_root: Path,
) -> list[dict[str, str | int]]:
    allowed = (
        ("source_snapshot", source_root.resolve()),
        ("isolated_environment", environment_root.resolve()),
        ("python_runtime", base_python_root.resolve()),
        ("windows", windows_root.resolve()),
    )
    host_packages = (base_python_root / "Lib/site-packages").resolve()
    records: list[dict[str, str | int]] = []
    for destination, original, kind in binaries:
        if kind not in {"BINARY", "EXTENSION"}:
            continue
        path = Path(original).resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"Native build input is not a file: {original}")
        if path.is_relative_to(host_packages) and not path.is_relative_to(
            environment_root.resolve()
        ):
            raise ValueError(f"Native dependency came from host site-packages: {path}")
        origin = next(
            (name for name, root in allowed if path.is_relative_to(root)), None
        )
        if origin is None:
            raise ValueError(
                f"Native dependency is outside the selected runtime: {path}"
            )
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        records.append(
            {
                "destination": destination,
                "source": str(path),
                "origin": origin,
                "size": path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        )
    return records
