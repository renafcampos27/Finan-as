"""Verify candidate bytes, retained source and actual packaged operations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.bundle_provenance import trusted_runtime_path


FUNCTIONAL_CHECKS = (
    "numpy_simulation",
    "numpy_health",
    "lgbk1_roundtrip",
    "lgbk2_roundtrip",
    "window_open_close",
    "pdf_preview",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory_digest(entries: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def safe_relative(value: str) -> str:
    if not isinstance(value, str) or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Invalid package path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(p in ("..", ".") for p in path.parts)
    ):
        raise ValueError("Invalid package path")
    if str(path) != value:
        raise ValueError("Non-canonical package path")
    return value


def validate_functional_report(
    report: dict[str, Any], *, expected_numpy: str | None = None
) -> None:
    if not isinstance(report, dict):
        raise ValueError("Functional evidence must be a JSON object")
    if report.get("ok") is not True or report.get("frozen") is not True:
        raise ValueError("Functional evidence did not run in the packaged executable")
    if (
        report.get("database_quick_check") != "ok"
        or int(report.get("page_exports_loaded", 0)) < 25
    ):
        raise ValueError("Incomplete database/page evidence")
    application_version = report.get("application_version")
    if not isinstance(application_version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", application_version):
        raise ValueError("Missing valid application version in functional evidence")
    # PDF runtime acceptance was introduced with 3.51. Retain the established
    # evidence contract for older published releases, while validating any
    # PDF evidence that they explicitly provide.
    requires_pdf = tuple(map(int, application_version.split("."))) >= (3, 51, 0)
    checks = report.get("functional_checks", {})
    required_checks = tuple(
        name for name in FUNCTIONAL_CHECKS if name != "pdf_preview" or requires_pdf
    )
    if not isinstance(checks, dict) or any(
        not isinstance(checks.get(name), dict) or checks[name].get("passed") is not True
        for name in required_checks
    ):
        raise ValueError("Missing successful functional operation evidence")
    for name, statistic in (("numpy_simulation", "median"), ("numpy_health", "score")):
        operation = checks[name]
        number = operation.get(statistic)
        if (
            not isinstance(number, (int, float))
            or isinstance(number, bool)
            or not math.isfinite(number)
        ):
            raise ValueError(f"Missing finite NumPy result: {name}")
        if (
            int(operation.get("rows", 0)) < (1 if name == "numpy_simulation" else 2)
            or int(operation.get("simulations", 0)) < 100
        ):
            raise ValueError(f"Incomplete NumPy operation: {name}")
    attachment_hashes = []
    for name, expected_format in (
        ("lgbk1_roundtrip", "LGBK1"),
        ("lgbk2_roundtrip", "LGBK2"),
    ):
        operation = checks[name]
        if (
            operation.get("format") != expected_format
            or operation.get("restored_marker") != "synthetic-acceptance"
            or operation.get("restored_transactions") != 12
        ):
            raise ValueError(f"Missing restored synthetic ledger evidence: {name}")
        if (
            operation.get("wrong_password_rejected") is not True
            or operation.get("truncated_rejected") is not True
        ):
            raise ValueError(f"Missing negative recovery evidence: {name}")
        attachment = operation.get("restored_attachment_sha256", "")
        if not isinstance(attachment, str) or not re.fullmatch(
            r"[a-fA-F0-9]{64}", attachment
        ):
            raise ValueError(f"Missing restored attachment evidence: {name}")
        attachment_hashes.append(attachment.lower())
    if len(set(attachment_hashes)) != 1:
        raise ValueError("Recovery formats did not preserve the same attachment")
    if checks["lgbk2_roundtrip"].get("kdf_algorithm") != "argon2id":
        raise ValueError("LGBK2 operation did not exercise the Argon2id KDF")
    if (
        checks["window_open_close"].get("shown") is not True
        or checks["window_open_close"].get("database_closed") is not True
    ):
        raise ValueError("Incomplete window lifecycle evidence")
    numpy_version = report.get("numpy")
    if not isinstance(numpy_version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)", numpy_version):
        raise ValueError("Unexpected NumPy runtime")
    if expected_numpy is not None and numpy_version != expected_numpy:
        raise ValueError("NumPy runtime differs from the verified source pin")
    pdf = checks.get("pdf_preview")
    if pdf is not None and (
        not isinstance(pdf, dict) or pdf.get("passed") is not True
        or
        type(pdf.get("document_pages")) is not int or pdf["document_pages"] != 1
        or type(pdf.get("render_width")) is not int or pdf["render_width"] != 240
        or type(pdf.get("render_height")) is not int or pdf["render_height"] != 320
        or type(pdf.get("rendered_marker_pixels")) is not int or pdf["rendered_marker_pixels"] < 20
        or type(pdf.get("viewer_marker_pixels")) is not int or pdf["viewer_marker_pixels"] < 20
        or pdf.get("viewer_shown") is not True
        or not isinstance(pdf.get("pdf_sha256"), str)
        or not re.fullmatch(r"[a-fA-F0-9]{64}", pdf["pdf_sha256"])
    ):
        raise ValueError("Incomplete PDF opening/rendering/viewer evidence")
    bundle_root = Path(report.get("bundle_root", ""))
    paths = report.get("bundle_module_paths", {})
    if not bundle_root.is_absolute() or not isinstance(paths, dict):
        raise ValueError("Missing frozen module provenance")
    required_modules = ("numpy", "argon2") + (("QtPdf", "QtPdfWidgets") if requires_pdf or pdf is not None else ())
    for name in required_modules:
        path = Path(paths.get(name, ""))
        if not path.is_absolute() or not path.is_relative_to(bundle_root):
            raise ValueError(f"{name} was not loaded from the packaged runtime")


def run_packaged_probe(executable: Path, *, timeout: float = 180) -> dict[str, Any]:
    """Exercise our explicitly selected executable with no host Python paths."""
    with tempfile.TemporaryDirectory(
        prefix="liquidglass-candidate-probe-"
    ) as temporary:
        root = Path(temporary)
        for folder in ("home", "local", "roaming", "tmp", "work"):
            (root / folder).mkdir()
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
        environment.update(
            FINANCAS_PESSOAIS_TESTING="1",
            FINANCAS_PESSOAIS_DEMO_DATA="0",
            FINANCAS_PESSOAIS_STATE_DIR=str(root / "profile"),
            LIQUIDGLASS_SELF_TEST_REPORT=str(root / "report.json"),
            QT_QPA_PLATFORM="offscreen",
            PYTHONNOUSERSITE="1",
            HOME=str(root / "home"),
            USERPROFILE=str(root / "home"),
            LOCALAPPDATA=str(root / "local"),
            APPDATA=str(root / "roaming"),
            TEMP=str(root / "tmp"),
            TMP=str(root / "tmp"),
            TMPDIR=str(root / "tmp"),
            PATH=trusted_runtime_path(Path(os.environ["SystemRoot"])),
        )
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
        completed = subprocess.run(
            [str(executable.resolve()), "--package-self-test"],
            cwd=root / "work",
            env=environment,
            capture_output=True,
            timeout=timeout,
            check=False,
            startupinfo=startup,
        )
        report_path = root / "report.json"
        if completed.returncode != 0 or not report_path.is_file():
            detail = (
                report_path.read_text(encoding="utf-8")
                if report_path.is_file()
                else "no report"
            )
            raise ValueError(
                f"Packaged executable failed ({completed.returncode}): {detail}; "
                f"stdout={completed.stdout.decode('utf-8', errors='replace')}; "
                f"stderr={completed.stderr.decode('utf-8', errors='replace')}"
            )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        validate_functional_report(report)
        return report


def verify_package(
    package: Path,
    *,
    source_root: Path | None = None,
    run_functional: bool = False,
    runtime_timeout: float = 180,
) -> dict[str, Any]:
    """Static checks are distinct from an explicitly requested fresh execution."""
    report: dict[str, Any] = {
        "ok": False,
        "integrity_ok": False,
        "source_ok": False,
        "functional_evidence_ok": False,
        "runtime_executed": False,
        "errors": [],
    }
    try:
        sidecars = (
            package.with_suffix(".zip.sha256.txt"),
            package.with_suffix(".sha256.txt"),
            package.with_suffix(".sha256"),
            package.with_suffix(".zip.sha256"),
        )
        sidecar = next((p for p in sidecars if p.is_file()), None)
        if sidecar is None:
            raise ValueError("Missing package SHA-256 sidecar")
        match = re.search(r"\b[0-9a-fA-F]{64}\b", sidecar.read_text(encoding="ascii"))
        if not match or match[0].lower() != sha256_file(package):
            raise ValueError("Package SHA-256 sidecar mismatch")
        with zipfile.ZipFile(package) as archive:
            files = [i for i in archive.infolist() if not i.is_dir()]
            names = [safe_relative(i.filename) for i in files]
            if len({name.casefold() for name in names}) != len(names):
                raise ValueError("Duplicate package paths")
            if any(stat.S_ISLNK(i.external_attr >> 16) for i in files):
                raise ValueError("Package contains a symbolic link")
            if sum(i.file_size for i in files) > 4 * 1024**3:
                raise ValueError("Package exceeds the inspection size limit")
            manifests = [
                n for n in names if PurePosixPath(n).name == "RELEASE_MANIFEST.json"
            ]
            if len(manifests) != 1:
                raise ValueError("Package requires exactly one release manifest")
            manifest_name = manifests[0]
            prefix = manifest_name.removesuffix("RELEASE_MANIFEST.json")
            if any(not name.startswith(prefix) for name in names):
                raise ValueError(
                    "Package contains files outside the application folder"
                )
            if archive.getinfo(manifest_name).file_size > 16 * 1024**2:
                raise ValueError("Release manifest exceeds its inspection limit")
            manifest = json.loads(archive.read(manifest_name).decode("utf-8-sig"))
            if not isinstance(manifest, dict):
                raise ValueError("Release manifest must be a JSON object")
            integrity = manifest.get("package_integrity", {})
            if not isinstance(integrity, dict):
                raise ValueError("Invalid package inventory")
            entries = integrity.get("entries", [])
            if (
                integrity.get("algorithm") != "SHA-256"
                or not isinstance(entries, list)
                or not entries
            ):
                raise ValueError("Missing package SHA-256 inventory")
            expected = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid package inventory entry")
                name = prefix + safe_relative(entry["path"])
                if name.casefold() in {n.casefold() for n in expected}:
                    raise ValueError("Duplicate package inventory entry")
                expected[name] = entry
            actual = set(names) - {manifest_name}
            if set(expected) != actual or len(entries) != integrity.get("file_count"):
                raise ValueError(
                    "Package file inventory does not match actual contents"
                )
            for name, entry in expected.items():
                digest = hashlib.sha256()
                with archive.open(name) as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                if (
                    archive.getinfo(name).file_size != entry.get("size")
                    or digest.hexdigest().lower()
                    != str(entry.get("sha256", "")).lower()
                ):
                    raise ValueError(f"Package integrity mismatch: {name}")
            executable = prefix + safe_relative(manifest.get("executable", ""))
            if (
                executable not in expected
                or str(expected[executable]["sha256"]).lower()
                != str(manifest.get("executable_sha256", "")).lower()
            ):
                raise ValueError("Executable fingerprint mismatch")
            report["integrity_ok"] = True

            source = manifest.get("source", {})
            if not isinstance(source, dict):
                raise ValueError("Invalid source inventory")
            source_entries = source.get("entries", [])
            if (
                not isinstance(source_entries, list)
                or not source_entries
                or inventory_digest(source_entries) != source.get("inventory_sha256")
            ):
                raise ValueError("Missing or inconsistent source inventory")
            directory = (
                source_root.resolve()
                if source_root
                else Path(source.get("snapshot_directory", ""))
            )
            if not directory.is_absolute() or not directory.is_dir():
                raise ValueError("Source snapshot is unavailable")
            seen = set()
            for entry in source_entries:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid source inventory entry")
                relative = safe_relative(entry["path"])
                if relative.casefold() in seen:
                    raise ValueError("Duplicate source inventory entry")
                seen.add(relative.casefold())
                path = (directory / relative).resolve()
                if not path.is_relative_to(directory.resolve()) or not path.is_file():
                    raise ValueError(
                        "Source snapshot path is unavailable or escapes its root"
                    )
                if (
                    path.stat().st_size != entry.get("size")
                    or sha256_file(path).lower() != str(entry.get("sha256", "")).lower()
                ):
                    raise ValueError(f"Source fingerprint mismatch: {relative}")
            if not {"main.py", "requirements.txt", "liquidglass.spec"}.issubset(seen):
                raise ValueError("Source inventory omits required build inputs")
            numpy_pin = re.search(
                r"^numpy==([0-9][0-9A-Za-z.+-]*)$",
                (directory / "requirements.txt").read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
            if numpy_pin is None:
                raise ValueError("Verified source does not pin the NumPy runtime")
            expected_numpy = numpy_pin.group(1)
            report["source_ok"] = True

            if manifest.get("self_test", {}).get("passed") is not True:
                raise ValueError("Manifest records a failed self-test")
            evidence_name = prefix + safe_relative(
                manifest.get("self_test", {}).get("report", "")
            )
            if evidence_name not in expected:
                raise ValueError(
                    "Functional evidence is absent from the byte inventory"
                )
            evidence = json.loads(archive.read(evidence_name))
            validate_functional_report(evidence, expected_numpy=expected_numpy)
            if evidence.get("application_version") != manifest.get("version"):
                raise ValueError(
                    "Functional evidence version differs from the packaged application"
                )
            report["functional_evidence_ok"] = True
            report["source"] = {
                key: source.get(key)
                for key in ("kind", "commit", "dirty", "inventory_sha256")
            }
            if run_functional:
                with tempfile.TemporaryDirectory(
                    prefix="liquidglass-candidate-extract-"
                ) as temporary:
                    destination = Path(temporary)
                    archive.extractall(destination)
                    report["runtime_report"] = run_packaged_probe(
                        destination / executable, timeout=runtime_timeout
                    )
                    validate_functional_report(report["runtime_report"], expected_numpy=expected_numpy)
                    report["runtime_executed"] = True
        report["ok"] = True
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        zipfile.BadZipFile,
        subprocess.TimeoutExpired,
    ) as exc:
        report["errors"].append(str(exc))
    return report
