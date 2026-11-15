"""Build an explicitly local candidate from a retained, hashed source snapshot.

This does not relax the official clean/tagged release build or replace release/.
Run with the Python interpreter of a freshly installed, isolated build venv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.package_integrity import (
    inventory_digest,
    run_packaged_probe,
    sha256_file,
    verify_package,
)
from scripts.bundle_provenance import trusted_build_path


ROOT_INPUTS = (
    "main.py",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "pytest.ini",
    "LiquidGlass.spec",
    "README.md",
    "COMO_INSTALAR_NOUTRO_PC.txt",
    "DATA_SAFETY.md",
    "RELEASE_NOTES.md",
    "RELEASE_NOTES_RESUME.md",
)
SOURCE_DIRECTORIES = (
    "database",
    "domain",
    "services",
    "ui",
    "utils",
    "scripts",
    "packaging",
)


def source_inputs(project: Path) -> list[Path]:
    paths = [project / name for name in ROOT_INPUTS]
    for name in SOURCE_DIRECTORIES:
        for path in (project / name).rglob("*"):
            if (
                path.is_file()
                and path.suffix in {".py", ".ps1", ".bat"}
                and "__pycache__" not in path.parts
            ):
                paths.append(path)
    for path in (project / "assets").rglob("*"):
        if path.is_file() and path.suffix.lower() in {
            ".qss",
            ".svg",
            ".png",
            ".ico",
            ".wav",
            ".mp3",
        }:
            paths.append(path)
    return sorted(set(paths))


def snapshot_source(project: Path, destination: Path) -> dict[str, Any]:
    """Copy only explicit build inputs; reject mutation during the copy."""
    destination.mkdir(parents=True, exist_ok=False)
    entries = []
    for source in source_inputs(project):
        if source.is_symlink() or not source.resolve().is_relative_to(
            project.resolve()
        ):
            raise ValueError(f"Build input escapes the source tree: {source}")
        relative = source.relative_to(project).as_posix()
        fingerprint = sha256_file(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if sha256_file(target) != fingerprint or sha256_file(source) != fingerprint:
            raise ValueError(f"Source changed during snapshot: {relative}")
        entries.append(
            {"path": relative, "size": target.stat().st_size, "sha256": fingerprint}
        )

    for entry in entries:
        if sha256_file(project / entry["path"]) != entry["sha256"]:
            raise ValueError(
                f"Source changed while snapshot was assembled: {entry['path']}"
            )

    def git(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", *args],
                cwd=project,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=15,
            ).strip()
        except subprocess.SubprocessError, OSError:
            return ""

    source = {
        "kind": "working_tree_snapshot",
        "commit": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "tag": None,
        "dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
        "snapshot_directory": str(destination.resolve()),
        "entries": entries,
        "inventory_sha256": inventory_digest(entries),
    }
    (destination.parent / "SOURCE_INVENTORY.json").write_text(
        json.dumps(source, indent=2), encoding="utf-8"
    )
    return source


def assert_snapshot(source: dict[str, Any]) -> None:
    directory = Path(source["snapshot_directory"])
    for entry in source["entries"]:
        path = directory / entry["path"]
        if (
            not path.is_file()
            or path.stat().st_size != entry["size"]
            or sha256_file(path) != entry["sha256"]
        ):
            raise ValueError(f"Retained build input changed: {entry['path']}")


def build_candidate(project: Path, output: Path) -> dict[str, Any]:
    project, output = project.resolve(), output.resolve()
    if os.name != "nt":
        raise ValueError("The validation candidate must be built on Windows")
    if not output.is_relative_to(project / "artifacts"):
        raise ValueError(
            "Local candidates must stay under the project's artifacts directory"
        )
    if output.exists():
        raise ValueError("Candidate output already exists; choose a new directory")
    if sys.prefix == sys.base_prefix:
        raise ValueError(
            "Use a dedicated build venv; the host Python is not a clean build environment"
        )
    config = Path(sys.prefix) / "pyvenv.cfg"
    if (
        "include-system-site-packages = false"
        not in config.read_text(encoding="utf-8").lower()
    ):
        raise ValueError("Build venv inherits host packages")
    output.mkdir(parents=True)
    print("Retaining source-only snapshot", flush=True)
    source = snapshot_source(project, output / "source")
    snapshot = Path(source["snapshot_directory"])
    version_namespace: dict[str, Any] = {}
    exec(
        compile(
            (snapshot / "utils/version.py").read_text(encoding="utf-8"),
            "version.py",
            "exec",
        ),
        version_namespace,
    )
    version_parts = tuple(
        int(part) for part in version_namespace["APP_VERSION"].split(".")
    ) + (0,)
    environment = os.environ.copy()
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "GEMINI_API_KEY",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "QML2_IMPORT_PATH",
        "QML_IMPORT_PATH",
    ):
        environment.pop(name, None)
    for folder in ("home", "local", "roaming", "tmp", "profile"):
        (output / "build-state" / folder).mkdir(parents=True)
    environment.update(
        FINANCAS_PESSOAIS_TESTING="1",
        FINANCAS_PESSOAIS_DEMO_DATA="0",
        FINANCAS_PESSOAIS_STATE_DIR=str(output / "build-state/profile"),
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
        QT_QPA_PLATFORM="offscreen",
        HOME=str(output / "build-state/home"),
        USERPROFILE=str(output / "build-state/home"),
        LOCALAPPDATA=str(output / "build-state/local"),
        APPDATA=str(output / "build-state/roaming"),
        TEMP=str(output / "build-state/tmp"),
        TMP=str(output / "build-state/tmp"),
        PYINSTALLER_CONFIG_DIR=str(output / "build-state/pyinstaller-cache"),
        PATH=trusted_build_path(
            Path(sys.executable), Path(sys.base_prefix), Path(os.environ["SystemRoot"])
        ),
    )
    runtime_probe = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import json,sys,struct,sysconfig;from importlib.metadata import version;"
            "print(json.dumps({'python':list(sys.version_info[:3]),'bits':struct.calcsize('P')*8,"
            "'free_threaded':bool(sysconfig.get_config_var('Py_GIL_DISABLED')),"
            "'packages':{name:version(name) for name in ('PySide6','cryptography','argon2-cffi','numpy','PyInstaller')}}))",
        ],
        cwd=snapshot,
        env=environment,
        text=True,
        timeout=30,
    )
    runtime = json.loads(runtime_probe)
    if (
        tuple(runtime["python"][:2]) != (3, 14)
        or tuple(runtime["python"]) < (3, 14, 7)
        or runtime["bits"] != 64
        or runtime["free_threaded"]
    ):
        raise ValueError(
            "Candidate requires standard 64-bit CPython 3.14.7 or later in 3.14"
        )
    pinned = {}
    for name in ("requirements.txt", "requirements-dev.txt"):
        pinned.update(
            re.findall(
                r"^([\w-]+)==([^\s]+)$",
                (snapshot / name).read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
        )
    for name, installed in runtime["packages"].items():
        if installed != pinned.get(name):
            raise ValueError(f"Build dependency does not match source pin: {name}")
    (output / "build_environment.json").write_text(
        json.dumps(runtime, indent=2), encoding="utf-8"
    )
    log_path = output / "build.log"
    with log_path.open("w", encoding="utf-8") as log:
        for command in (
            [sys.executable, "-m", "pip", "check"],
            [sys.executable, "-m", "pip", "freeze", "--all"],
        ):
            subprocess.run(
                command,
                cwd=snapshot,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
        # Resource metadata belongs to generated work, not retained source.
        (snapshot / "build").mkdir()
        (snapshot / "build/version_info.txt").write_text(
            f"VSVersionInfo(ffi=FixedFileInfo(filevers={version_parts!r}, prodvers={version_parts!r}, mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0,0)), kids=[])",
            encoding="utf-8",
        )
        print("Building frozen executable in the isolated environment", flush=True)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "LiquidGlass.spec",
            ],
            cwd=snapshot,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    assert_snapshot(source)
    shutil.copyfile(
        snapshot / "build/BINARY_PROVENANCE.json", output / "BINARY_PROVENANCE.json"
    )
    bundle = output / "LiquidGlass"
    shutil.copytree(snapshot / "dist/LiquidGlass", bundle)
    print("Executing real frozen operations from the retained bundle", flush=True)
    evidence = run_packaged_probe(bundle / "LiquidGlass.exe")
    evidence_dir = bundle / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "package_self_test.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )
    for name in (
        "README.md",
        "COMO_INSTALAR_NOUTRO_PC.txt",
        "DATA_SAFETY.md",
        "RELEASE_NOTES.md",
        "RELEASE_NOTES_RESUME.md",
    ):
        shutil.copyfile(snapshot / name, bundle / name)
    for name in (
        "INSTALAR_FINANCAS_PESSOAIS.bat",
        "INSTALAR_FINANCAS_PESSOAIS.ps1",
        "APLICAR_ATUALIZACAO.ps1",
        "ABRIR_FINANCAS_PESSOAIS.bat",
    ):
        shutil.copyfile(snapshot / "packaging/windows" / name, bundle / name)
    for path in bundle.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in {
            ".sqlite",
            ".sqlite3",
            ".db",
            ".lgbak",
            ".key",
            ".log",
        } or path.name.startswith(".env"):
            raise ValueError(
                f"Unexpected private/state file in candidate: {path.relative_to(bundle)}"
            )
    entries = [
        {
            "path": p.relative_to(bundle).as_posix(),
            "size": p.stat().st_size,
            "sha256": sha256_file(p),
        }
        for p in sorted(bundle.rglob("*"))
        if p.is_file()
    ]
    manifest = {
        "product": "Finanças Pessoais — Liquid Glass",
        "version": version_namespace["APP_VERSION"],
        "distribution_kind": "local_validation_candidate",
        "source": source,
        "executable": "LiquidGlass.exe",
        "executable_sha256": sha256_file(bundle / "LiquidGlass.exe"),
        "target": {
            "operating_system": "Windows",
            "bits": 64,
            "requires_python_on_target": False,
        },
        "self_test": {"passed": True, "report": "evidence/package_self_test.json"},
        "installation": {
            "requires_admin": False,
            "dependencies_embedded": True,
            "install_test": {"passed": False},
        },
        "package_integrity": {
            "algorithm": "SHA-256",
            "file_count": len(entries),
            "entries": entries,
        },
    }
    (bundle / "RELEASE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    package = (
        output
        / f"LiquidGlass_App_v{version_namespace['APP_VERSION']}_phase2_candidate.zip"
    )
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(
                    path, "LiquidGlass/" + path.relative_to(bundle).as_posix()
                )
    package.with_suffix(".zip.sha256.txt").write_text(
        f"{sha256_file(package)}  {package.name}\n", encoding="ascii"
    )
    print("Extracting delivered ZIP and repeating the frozen operations", flush=True)
    gate = verify_package(package, source_root=snapshot, run_functional=True)
    (output / "candidate_validation.json").write_text(
        json.dumps(gate, indent=2), encoding="utf-8"
    )
    assert_snapshot(source)
    if not gate["ok"] or not gate["runtime_executed"]:
        raise ValueError(
            "Candidate failed delivered-package validation: "
            + "; ".join(gate["errors"])
        )
    return {
        "package": str(package),
        "source": str(snapshot),
        "validation": str(output / "candidate_validation.json"),
        "sha256": sha256_file(package),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_candidate(args.project_root, args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
