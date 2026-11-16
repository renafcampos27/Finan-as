"""Council plan / A02: verify bytes, available source and frozen operations."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts.build_validation_candidate import snapshot_source
from scripts.package_integrity import FUNCTIONAL_CHECKS, inventory_digest, sha256_file
from scripts.release_gate import verify_candidate_package, verify_release_gate


def _report(root: Path) -> dict:
    bundle = root / "build-bundle" / "_internal"
    return {
        "ok": True,
        "frozen": True,
        "application_version": "3.49.0",
        "database_quick_check": "ok",
        "page_exports_loaded": 30,
        "numpy": "2.5.2",
        "functional_checks": {
            "numpy_simulation": {
                "passed": True,
                "median": 1000.0,
                "rows": 1,
                "simulations": 200,
            },
            "numpy_health": {
                "passed": True,
                "score": 50,
                "rows": 4,
                "simulations": 200,
            },
            **{
                name: {
                    "passed": True,
                    "format": format_name,
                    "restored_marker": "synthetic-acceptance",
                    "restored_transactions": 12,
                    "wrong_password_rejected": True,
                    "truncated_rejected": True,
                    "restored_attachment_sha256": "a" * 64,
                    **({"kdf_algorithm": "argon2id"} if format_name == "LGBK2" else {}),
                }
                for name, format_name in (
                    ("lgbk1_roundtrip", "LGBK1"),
                    ("lgbk2_roundtrip", "LGBK2"),
                )
            },
            "window_open_close": {
                "passed": True,
                "shown": True,
                "database_closed": True,
            },
            "pdf_preview": {
                "passed": True, "document_pages": 1,
                "render_width": 240, "render_height": 320,
                "rendered_marker_pixels": 100, "viewer_marker_pixels": 100,
                "viewer_shown": True, "pdf_sha256": "b" * 64,
            },
        },
        "bundle_root": str(bundle),
        "bundle_module_paths": {
            name: str(bundle / name / "__init__.pyc") for name in ("numpy", "argon2", "QtPdf", "QtPdfWidgets")
        },
    }


def _write_package(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    path.with_suffix(".zip.sha256.txt").write_text(sha256_file(path), encoding="ascii")


def _package(root: Path) -> tuple[Path, dict[str, bytes]]:
    source = root / "source"
    source.mkdir()
    for name in ("main.py", "requirements.txt", "LiquidGlass.spec"):
        (source / name).write_text(
            "numpy==2.5.2\n" if name == "requirements.txt" else "synthetic build input",
            encoding="utf-8",
        )
    source_entries = [
        {"path": p.name, "size": p.stat().st_size, "sha256": sha256_file(p)}
        for p in sorted(source.iterdir())
    ]
    files = {
        "LiquidGlass/LiquidGlass.exe": b"synthetic executable; never execute this fixture",
        "LiquidGlass/evidence/package_self_test.json": json.dumps(
            _report(root)
        ).encode(),
    }
    entries = [
        {
            "path": name.removeprefix("LiquidGlass/"),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for name, data in files.items()
    ]
    manifest = {
        "version": "3.49.0",
        "distribution_kind": "local_validation_candidate",
        "source": {
            "kind": "working_tree_snapshot",
            "dirty": True,
            "snapshot_directory": str(source),
            "entries": source_entries,
            "inventory_sha256": inventory_digest(source_entries),
        },
        "executable": "LiquidGlass.exe",
        "executable_sha256": entries[0]["sha256"],
        "self_test": {"passed": True, "report": "evidence/package_self_test.json"},
        "package_integrity": {
            "algorithm": "SHA-256",
            "file_count": len(entries),
            "entries": entries,
        },
    }
    files["LiquidGlass/RELEASE_MANIFEST.json"] = json.dumps(manifest).encode()
    package = root / "candidate.zip"
    _write_package(package, files)
    return package, files


def _update_manifest(files: dict[str, bytes], callback) -> None:
    name = "LiquidGlass/RELEASE_MANIFEST.json"
    manifest = json.loads(files[name])
    callback(manifest)
    files[name] = json.dumps(manifest).encode()


def test_retained_source_and_full_byte_inventory_pass_static_inspection(tmp_path):
    package, _files = _package(tmp_path)
    report = verify_candidate_package(package)
    assert report["ok"], report
    assert (
        report["integrity_ok"]
        and report["source_ok"]
        and report["functional_evidence_ok"]
    )
    assert report["runtime_executed"] is False


def test_probe_keeps_windows_environment_case_handling_and_discards_foreign_path(
    tmp_path, monkeypatch
):
    import os
    from types import SimpleNamespace
    from scripts import package_integrity
    from scripts.bundle_provenance import trusted_runtime_path

    monkeypatch.setenv("PATH", str(tmp_path / "foreign/native/poppler"))
    monkeypatch.setenv("QT_PLUGIN_PATH", str(tmp_path / "foreign/qt"))

    def fake_run(_command, *, env, **_kwargs):
        assert env["PATH"] == trusted_runtime_path(Path(os.environ["SystemRoot"]))
        assert "QT_PLUGIN_PATH" not in env
        Path(env["LIQUIDGLASS_SELF_TEST_REPORT"]).write_text(
            json.dumps(_report(tmp_path)), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(package_integrity.subprocess, "run", fake_run)
    assert package_integrity.run_packaged_probe(tmp_path / "synthetic.exe")["ok"]


def test_passed_boolean_without_byte_source_operation_evidence_is_rejected(tmp_path):
    package = tmp_path / "forged.zip"
    _write_package(
        package,
        {
            "RELEASE_MANIFEST.json": json.dumps(
                {
                    "self_test": {"passed": True},
                    "installation": {"install_test": {"passed": True}},
                }
            ).encode()
        },
    )
    report = verify_candidate_package(package)
    assert not report["ok"]
    assert "inventory" in report["errors"][0]


@pytest.mark.parametrize(
    "mutation",
    [
        "changed",
        "missing",
        "extra",
        "duplicate",
        "traversal",
        "module_path",
        "operation",
        "wrong_version",
        "booleans_only",
        "nonfinite_result",
        "kdf_fallback",
        "invalid_recovery_accepted",
        "blank_pdf",
        "unshown_pdf",
        "external_pdf_module",
        "numpy_version",
    ],
)
def test_manifest_flags_do_not_override_invalid_actual_contents(tmp_path, mutation):
    package, files = _package(tmp_path)
    if mutation == "changed":
        files["LiquidGlass/LiquidGlass.exe"] += b"changed"
    elif mutation == "missing":
        del files["LiquidGlass/LiquidGlass.exe"]
    elif mutation == "extra":
        files["LiquidGlass/unlisted.txt"] = b"unlisted"
    elif mutation == "duplicate":
        files["LiquidGlass/LIQUIDGLASS.exe"] = b"case collision"
    elif mutation == "traversal":
        files["LiquidGlass/../outside.txt"] = b"escape"
    else:
        evidence_name = "LiquidGlass/evidence/package_self_test.json"
        evidence = json.loads(files[evidence_name])
        if mutation == "module_path":
            evidence["bundle_module_paths"]["numpy"] = str(
                tmp_path / "host-python/numpy/__init__.py"
            )
        elif mutation == "operation":
            del evidence["functional_checks"]["lgbk2_roundtrip"]
        elif mutation == "booleans_only":
            evidence["functional_checks"] = {
                name: {"passed": True} for name in FUNCTIONAL_CHECKS
            }
        elif mutation == "nonfinite_result":
            evidence["functional_checks"]["numpy_simulation"]["median"] = float("nan")
        elif mutation == "kdf_fallback":
            evidence["functional_checks"]["lgbk2_roundtrip"]["kdf_algorithm"] = "scrypt"
        elif mutation == "invalid_recovery_accepted":
            evidence["functional_checks"]["lgbk2_roundtrip"][
                "wrong_password_rejected"
            ] = False
        elif mutation == "blank_pdf":
            evidence["functional_checks"]["pdf_preview"]["rendered_marker_pixels"] = 0
        elif mutation == "unshown_pdf":
            evidence["functional_checks"]["pdf_preview"]["viewer_shown"] = False
        elif mutation == "external_pdf_module":
            evidence["bundle_module_paths"]["QtPdf"] = str(tmp_path / "foreign/QtPdf.pyd")
        elif mutation == "numpy_version":
            evidence["numpy"] = "0.0.0"
        else:
            evidence["application_version"] = "99.0.0"
        files[evidence_name] = json.dumps(evidence).encode()

        def refresh(manifest):
            for entry in manifest["package_integrity"]["entries"]:
                if entry["path"] == "evidence/package_self_test.json":
                    entry.update(
                        size=len(files[evidence_name]),
                        sha256=hashlib.sha256(files[evidence_name]).hexdigest(),
                    )

        _update_manifest(files, refresh)
    _write_package(package, files)
    assert verify_candidate_package(package)["ok"] is False


@pytest.mark.parametrize("application_version,requires_pdf", [("3.50.1", False), ("3.51.0", True)])
def test_pdf_proof_is_required_from_351_without_invalidating_published_3501(
    tmp_path, application_version, requires_pdf
):
    from scripts.package_integrity import validate_functional_report

    report = _report(tmp_path)
    report["application_version"] = application_version
    del report["functional_checks"]["pdf_preview"]
    del report["bundle_module_paths"]["QtPdf"]
    del report["bundle_module_paths"]["QtPdfWidgets"]
    if requires_pdf:
        with pytest.raises(ValueError, match="functional operation"):
            validate_functional_report(report, expected_numpy="2.5.2")
    else:
        validate_functional_report(report, expected_numpy="2.5.2")


@pytest.mark.parametrize("state", ["modified", "unavailable"])
def test_source_must_remain_available_and_match_fingerprints(tmp_path, state):
    package, _files = _package(tmp_path)
    if state == "modified":
        (tmp_path / "source/main.py").write_text("modified", encoding="utf-8")
    else:
        (tmp_path / "source").rename(tmp_path / "missing-source")
    report = verify_candidate_package(package)
    assert report["integrity_ok"] is True
    assert report["source_ok"] is False
    assert report["ok"] is False


def test_whole_zip_checksum_is_verified(tmp_path):
    package, _files = _package(tmp_path)
    package.with_suffix(".zip.sha256.txt").write_text("0" * 64, encoding="ascii")
    assert verify_candidate_package(package)["ok"] is False


def test_fresh_execution_uses_the_extracted_delivered_executable(tmp_path, monkeypatch):
    from scripts import package_integrity

    package, files = _package(tmp_path)
    called = []

    def fake_probe(executable, *, timeout):
        assert executable.read_bytes() == files["LiquidGlass/LiquidGlass.exe"]
        assert not executable.is_relative_to(tmp_path)
        called.append(executable)
        return _report(tmp_path)

    monkeypatch.setattr(package_integrity, "run_packaged_probe", fake_probe)
    report = verify_candidate_package(package, run_functional=True)
    assert report["ok"] and report["runtime_executed"]
    assert len(called) == 1
    assert not called[0].exists()


def test_source_snapshot_excludes_persistent_and_unrelated_files(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    from scripts.build_validation_candidate import ROOT_INPUTS

    for name in ROOT_INPUTS:
        (project / name).write_text("synthetic input", encoding="utf-8")
    (project / "services").mkdir()
    (project / "services/example.py").write_text("VALUE=1", encoding="utf-8")
    (project / "services/private.sqlite3").write_bytes(b"personal")
    (project / "backups").mkdir()
    (project / "backups/private.lgbak").write_bytes(b"personal")
    (project / "financas_pessoais.sqlite3").write_bytes(b"personal")
    source = snapshot_source(project, tmp_path / "retained-source")
    names = {entry["path"] for entry in source["entries"]}
    assert "services/example.py" in names
    assert all(not name.endswith((".sqlite3", ".lgbak")) for name in names)
    assert source["inventory_sha256"] == inventory_digest(source["entries"])


def test_strict_official_gate_retains_source_release_policy(tmp_path):
    root = tmp_path / "project"
    (root / "utils").mkdir(parents=True)
    (root / "utils/version.py").write_text(
        'APP_VERSION="3.49.0"\nAPP_VERSION_LABEL="v3.49.0"\nRELEASE_VERSION="V3.49"\n',
        encoding="utf-8",
    )
    for name in ("RELEASE_NOTES.md", "RELEASE_NOTES_RESUME.md"):
        (root / name).write_text("## V3.49 — v3.49.0 — release\n", encoding="utf-8")
    (root / "release").mkdir()
    package, files = _package(root)
    _write_package(root / "release/LiquidGlass_App_v3.49.0.zip", files)
    report = verify_release_gate(root, strict=True)
    # A locally labelled dirty candidate cannot stand in for official provenance.
    assert report["ok"] is False
    assert any("dirty/local" in error for error in report["errors"])
