#!/usr/bin/env python3
"""Unified release gate and provenance verification script (AUD-37).

Validates consistency across:
1. Canonical application version in utils/version.py.
2. Release documentation headings in RELEASE_NOTES.md and RELEASE_NOTES_RESUME.md.
3. Distribution packages and SHA-256 sidecar checksums in release/.
4. Release manifest metadata (RELEASE_MANIFEST.json) inside zip packages.
5. Git tag and source commit provenance when in a git checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.package_integrity import verify_package


def verify_candidate_package(
    package: Path, *, source_root: Path | None = None, run_functional: bool = False,
    runtime_timeout: float = 180,
) -> dict[str, Any]:
    """Inspect a local candidate without claiming clean official release status."""
    return verify_package(
        package, source_root=source_root, run_functional=run_functional,
        runtime_timeout=runtime_timeout,
    )


def _compute_sha256(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest().upper()


def _extract_headings(markdown_path: Path) -> list[dict[str, Any]]:
    if not markdown_path.is_file():
        return []
    text = markdown_path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^##\s+(V\d+\.\d+)\s+[—\-]\s+v?([0-9a-zA-Z\.\-]+)\s+[—\-]\s*(.*)$",
        re.MULTILINE,
    )
    headings = []
    for match in pattern.finditer(text):
        rel_ver = match.group(1).strip()
        ver_str = match.group(2).strip()
        desc = match.group(3).strip()
        is_unreleased = bool(
            re.search(
                r"unreleased|não é um lançamento|em curso",
                f"{ver_str} {desc}",
                re.IGNORECASE,
            )
        )
        headings.append(
            {
                "release_version": rel_ver,
                "version": ver_str,
                "description": desc,
                "is_unreleased": is_unreleased,
            }
        )
    return headings


def _extract_first_heading(markdown_path: Path) -> tuple[str, str] | None:
    """Legacy helper returning (release_version, version) for the top heading."""
    headings = _extract_headings(markdown_path)
    if headings:
        return headings[0]["release_version"], headings[0]["version"]
    return None


def verify_release_gate(
    project_root: Path | None = None,
    strict_git: bool = False,
    strict: bool = False,
    candidate: str | Path | None = None,
    allow_unreleased: bool = False,
    release_directory: str | Path | None = None,
) -> dict[str, Any]:
    """Execute all release gate verifications.

    In diagnostic mode (strict=False), provides warnings and checks for development.
    In strict mode (strict=True), enforces that the release candidate exists, matches
    checksum, has valid manifest and provenance, clean git state, and required self-tests.
    """
    root = Path(project_root or Path(__file__).resolve().parents[1])
    report: dict[str, Any] = {
        "ok": True,
        "strict": strict,
        "version": "",
        "release_version": "",
        "candidate_package": None,
        "checks_passed": [],
        "warnings": [],
        "errors": [],
        "manifest_source": None,
        "git_status": None,
        "packages": [],
    }

    # 1. Check version definitions in utils/version.py without module cache contamination
    version_file = root / "utils" / "version.py"
    if not version_file.is_file():
        report["errors"].append(f"Missing version file at {version_file}")
        report["ok"] = False
        return report

    try:
        content = version_file.read_text(encoding="utf-8")
        ns: dict[str, Any] = {}
        code = compile(content, str(version_file), "exec")
        exec(code, ns)
        app_version = str(ns.get("APP_VERSION", ""))
        app_version_label = str(ns.get("APP_VERSION_LABEL", ""))
        release_version = str(ns.get("RELEASE_VERSION", ""))

        report["version"] = app_version
        report["release_version"] = release_version
        if not re.fullmatch(r"\d+\.\d+\.\d+", app_version):
            report["errors"].append(f"APP_VERSION '{app_version}' does not follow semver (X.Y.Z).")
        elif app_version_label != f"v{app_version}":
            report["errors"].append(f"APP_VERSION_LABEL mismatch: {app_version_label} != v{app_version}")
        else:
            report["checks_passed"].append(f"Version metadata valid: {app_version} ({release_version})")
    except Exception as exc:
        report["errors"].append(f"Failed to load utils/version.py: {exc}")
        report["ok"] = False
        return report

    expected_version = report["version"]
    expected_rel_version = report["release_version"]

    # 2. Check RELEASE_NOTES.md and RELEASE_NOTES_RESUME.md
    headings_by_doc: dict[str, list[dict[str, Any]]] = {}
    for doc_name in ("RELEASE_NOTES.md", "RELEASE_NOTES_RESUME.md"):
        doc_path = root / doc_name
        headings = _extract_headings(doc_path)
        headings_by_doc[doc_name] = headings
        if not headings:
            report["errors"].append(f"Missing or invalid release headings in {doc_name}")
            continue

        top = headings[0]
        if top["is_unreleased"]:
            report["checks_passed"].append(
                f"{doc_name} current top heading is unreleased: {top['release_version']} — {top['version']}"
            )
            clean_top_ver = re.sub(r"-?unreleased.*", "", top["version"], flags=re.IGNORECASE)
            if expected_version == clean_top_ver or expected_version == top["version"]:
                if strict and not allow_unreleased:
                    report["errors"].append(
                        f"{doc_name} top heading is marked unreleased ({top['version']}). "
                        f"Finalize release notes or specify allow_unreleased=True."
                    )
            else:
                matching_heading = next(
                    (
                        h
                        for h in headings
                        if h["version"] == expected_version
                        or re.sub(r"-?unreleased.*", "", h["version"], flags=re.IGNORECASE) == expected_version
                    ),
                    None,
                )
                if not matching_heading:
                    report["errors"].append(
                        f"{doc_name} does not contain any heading for expected version {expected_version}"
                    )
                elif matching_heading["release_version"] != expected_rel_version:
                    report["errors"].append(
                        f"{doc_name} heading for {expected_version} has release_version "
                        f"'{matching_heading['release_version']}' != '{expected_rel_version}'"
                    )
                else:
                    report["checks_passed"].append(
                        f"{doc_name} heading matches expected version {expected_version}"
                    )
        else:
            if (top["release_version"], top["version"]) != (expected_rel_version, expected_version):
                report["errors"].append(
                    f"{doc_name} top heading ({top['release_version']}, {top['version']}) does not match expected "
                    f"({expected_rel_version}, {expected_version})"
                )
            else:
                report["checks_passed"].append(f"{doc_name} first heading matches {expected_version}")

    # Consistency check between both documentation files
    if (
        "RELEASE_NOTES.md" in headings_by_doc
        and "RELEASE_NOTES_RESUME.md" in headings_by_doc
        and headings_by_doc["RELEASE_NOTES.md"]
        and headings_by_doc["RELEASE_NOTES_RESUME.md"]
    ):
        top_tech = headings_by_doc["RELEASE_NOTES.md"][0]
        top_res = headings_by_doc["RELEASE_NOTES_RESUME.md"][0]
        if (top_tech["release_version"], top_tech["version"]) != (
            top_res["release_version"],
            top_res["version"],
        ):
            report["errors"].append(
                f"Release notes heading mismatch: RELEASE_NOTES.md has ({top_tech['release_version']}, {top_tech['version']}) "
                f"while RELEASE_NOTES_RESUME.md has ({top_res['release_version']}, {top_res['version']})"
            )

    # 3. Check release artifacts in release/
    release_dir = Path(release_directory) if release_directory is not None else root / "release"
    if not release_dir.is_absolute():
        release_dir = root / release_dir
    selected_candidate_pkg: dict[str, Any] | None = None
    if release_dir.is_dir():
        zip_files = sorted(release_dir.glob("*.zip"))
        for zip_path in zip_files:
            pkg_info: dict[str, Any] = {
                "file": zip_path.name,
                "size": zip_path.stat().st_size,
                "sha256": "",
                "sidecar_matched": False,
                "manifest_found": False,
                "manifest_version": "",
                "manifest_source": None,
                "manifest_self_test_passed": False,
                "is_candidate": False,
            }

            # Check SHA-256 sidecar
            computed_sha = _compute_sha256(zip_path)
            pkg_info["sha256"] = computed_sha

            sidecar_candidates = [
                zip_path.parent / (zip_path.stem + ".sha256.txt"),
                zip_path.with_suffix(".zip.sha256.txt"),
                zip_path.with_suffix(".sha256"),
                zip_path.parent / (zip_path.stem + ".sha256"),
            ]
            sidecar_path = next((p for p in sidecar_candidates if p.is_file()), None)

            if sidecar_path is not None:
                sidecar_content = sidecar_path.read_text(encoding="ascii", errors="replace").strip()
                hash_match = re.search(r"\b[A-Fa-f0-9]{64}\b", sidecar_content)
                sidecar_hash = hash_match.group(0).upper() if hash_match else ""
                if sidecar_hash == computed_sha:
                    pkg_info["sidecar_matched"] = True
                    report["checks_passed"].append(f"{zip_path.name} matches {sidecar_path.name} SHA-256")
                else:
                    pkg_info["sidecar_matched"] = False
                    report["errors"].append(
                        f"Checksum mismatch for {zip_path.name}: computed {computed_sha}, "
                        f"sidecar has {sidecar_hash}"
                    )
            else:
                pkg_info["sidecar_matched"] = None
                report["warnings"].append(f"No SHA-256 sidecar found for {zip_path.name}")

            # Inspect zip contents
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    namelist = zf.namelist()
                    manifest_candidates = [n for n in namelist if n.endswith("RELEASE_MANIFEST.json")]
                    if manifest_candidates:
                        pkg_info["manifest_found"] = True
                        manifest_raw = zf.read(manifest_candidates[0]).decode("utf-8-sig")
                        manifest_data = json.loads(manifest_raw)
                        manifest_ver = manifest_data.get("version", "")
                        pkg_info["manifest_version"] = manifest_ver
                        pkg_info["manifest_source"] = manifest_data.get("source")
                        # Preserve last manifest_source on top level for backward compatibility
                        report["manifest_source"] = manifest_data.get("source")

                        if manifest_ver == expected_version:
                            report["checks_passed"].append(
                                f"{zip_path.name} manifest version matches {expected_version}"
                            )
                        else:
                            report["warnings"].append(
                                f"{zip_path.name} manifest version ({manifest_ver}) != {expected_version}"
                            )

                        # Check self-test / install_test in manifest
                        install_test = manifest_data.get("installation", {}).get("install_test", {})
                        if install_test.get("passed") is True:
                            pkg_info["manifest_self_test_passed"] = True
                            report["checks_passed"].append(
                                f"{zip_path.name} installation self-test verified (passed=True)"
                            )
                        else:
                            pkg_info["manifest_self_test_passed"] = False
                            report["warnings"].append(
                                f"{zip_path.name} installation self-test not marked passed in manifest"
                            )
                    else:
                        report["warnings"].append(f"No RELEASE_MANIFEST.json found in {zip_path.name}")
            except Exception as exc:
                report["errors"].append(f"Failed to read zip {zip_path.name}: {exc}")

            report["packages"].append(pkg_info)

        # Resolve candidate package with explicit precedence
        if candidate:
            cand_str = str(candidate)
            for pkg in report["packages"]:
                if (
                    pkg["file"] == cand_str
                    or Path(pkg["file"]).stem == cand_str
                    or pkg["file"] == Path(cand_str).name
                ):
                    selected_candidate_pkg = pkg
                    break
        else:
            # Priority 1: Canonical versioned name
            for pkg in report["packages"]:
                if pkg["file"] == f"LiquidGlass_App_v{expected_version}.zip":
                    selected_candidate_pkg = pkg
                    break
            # Priority 2: Version label in filename and manifest matches
            if not selected_candidate_pkg:
                for pkg in report["packages"]:
                    if (
                        f"v{expected_version}" in pkg["file"]
                        and pkg.get("manifest_version") == expected_version
                    ):
                        selected_candidate_pkg = pkg
                        break
            # Priority 3: Manifest matches
            if not selected_candidate_pkg:
                for pkg in report["packages"]:
                    if pkg.get("manifest_version") == expected_version:
                        selected_candidate_pkg = pkg
                        break

        if selected_candidate_pkg:
            selected_candidate_pkg["is_candidate"] = True
            report["candidate_package"] = selected_candidate_pkg["file"]
    else:
        report["warnings"].append(f"Release directory {release_dir} does not exist.")

    if selected_candidate_pkg:
        report["candidate_package"] = selected_candidate_pkg["file"]

    if strict:
        if not selected_candidate_pkg:
            report["errors"].append(
                f"Strict release gate: No valid release candidate package found for v{expected_version}."
            )
        else:
            cand_name = selected_candidate_pkg["file"]
            candidate_source = selected_candidate_pkg.get("manifest_source") or {}
            if not isinstance(candidate_source, dict):
                candidate_source = {}
            if candidate_source.get("dirty") is not False or candidate_source.get("kind") != "git_release_source":
                report["errors"].append(
                    "Strict release gate: A dirty/local validation candidate is not an official release source."
                )
            if selected_candidate_pkg["sidecar_matched"] is not True:
                report["errors"].append(
                    f"Strict release gate: Candidate package {cand_name} missing or invalid SHA-256 sidecar."
                )
            if not selected_candidate_pkg["manifest_found"]:
                report["errors"].append(
                    f"Strict release gate: Candidate package {cand_name} missing RELEASE_MANIFEST.json."
                )
            if selected_candidate_pkg["manifest_version"] != expected_version:
                report["errors"].append(
                    f"Strict release gate: Candidate package {cand_name} manifest version "
                    f"'{selected_candidate_pkg['manifest_version']}' != '{expected_version}'."
                )
            if not selected_candidate_pkg["manifest_self_test_passed"]:
                report["errors"].append(
                    f"Strict release gate: Candidate package {cand_name} has no verified install self-test evidence."
                )
            validation = verify_candidate_package(release_dir / cand_name, source_root=root)
            selected_candidate_pkg["content_validation"] = validation
            for error in validation["errors"]:
                report["errors"].append(f"Strict release gate: {error}")
            if validation["ok"]:
                report["checks_passed"].append(
                    f"{cand_name} byte inventory, retained source and functional evidence verified"
                )

    # 4. Check Git provenance if repository exists
    git_dir = root / ".git"
    if git_dir.exists():
        try:
            head_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
                timeout=10,
                stderr=subprocess.DEVNULL,
            ).strip()
            status_out = subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=root,
                text=True,
                timeout=10,
                stderr=subprocess.DEVNULL,
            ).strip()
            is_dirty = bool(status_out)

            tag_commit = ""
            try:
                tag_commit = subprocess.check_output(
                    ["git", "rev-parse", f"refs/tags/v{expected_version}^{{commit}}"],
                    cwd=root,
                    text=True,
                    timeout=10,
                    stderr=subprocess.DEVNULL,
                ).strip()
            except Exception:
                pass

            git_info = {
                "head": head_commit,
                "tag": f"v{expected_version}",
                "tag_commit": tag_commit,
                "is_dirty": is_dirty,
            }
            report["git_status"] = git_info

            if tag_commit:
                report["checks_passed"].append(
                    f"Git tag v{expected_version} resolves to commit {tag_commit[:8]}"
                )
                if strict and tag_commit != head_commit:
                    report["errors"].append(
                        "Strict release gate: Release tag does not identify the current HEAD."
                    )
            else:
                if strict:
                    report["errors"].append(
                        f"Strict release gate: Missing git tag v{expected_version} for candidate release."
                    )
                else:
                    report["warnings"].append(f"Git tag v{expected_version} does not exist in local repo.")

            # Cross-reference candidate manifest source commit if present
            cand_source = (
                selected_candidate_pkg.get("manifest_source") if selected_candidate_pkg else None
            )
            if cand_source and isinstance(cand_source, dict):
                manifest_commit = cand_source.get("commit", "")
                if manifest_commit:
                    if tag_commit and manifest_commit != tag_commit:
                        msg = (
                            f"Provenance divergence: Candidate manifest was built from commit {manifest_commit[:8]}, "
                            f"but git tag v{expected_version} points to {tag_commit[:8]}"
                        )
                        if strict:
                            report["errors"].append(f"Strict release gate: {msg}")
                        else:
                            report["warnings"].append(msg)
                    else:
                        report["checks_passed"].append(
                            f"Candidate manifest commit ({manifest_commit[:8]}) matches tag provenance."
                        )

            if (strict_git or strict) and is_dirty:
                report["errors"].append("Git working tree has uncommitted modifications.")
        except Exception as exc:
            if strict or strict_git:
                report["errors"].append(f"Strict release gate: Git inspection failed: {exc}")
            else:
                report["warnings"].append(f"Git inspection skipped: {exc}")

    elif strict or strict_git:
        report["errors"].append("A Git checkout is required for strict provenance verification.")

    report["ok"] = len(report["errors"]) == 0
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify release provenance and integrity gate (AUD-37, C9).")
    parser.add_argument("--project-root", type=Path, default=None, help="Root path of the project")
    parser.add_argument("--strict", action="store_true", help="Enforce strict release candidate requirements")
    parser.add_argument("--strict-git", action="store_true", help="Fail if git working tree is dirty")
    parser.add_argument("--candidate", type=str, default=None, help="Specific candidate zip filename or stem")
    parser.add_argument("--release-dir", type=Path, default=None, help="Package directory, relative to project root or absolute")
    parser.add_argument("--allow-unreleased", action="store_true", help="Allow unreleased top documentation heading")
    parser.add_argument("--json", action="store_true", help="Output report as JSON")
    parser.add_argument("--validate-package", type=Path, help="Inspect an explicit local candidate ZIP")
    parser.add_argument("--source-root", type=Path, help="Retained source snapshot for that candidate")
    parser.add_argument("--run-functional", action="store_true", help="Execute the selected trusted package in a temporary profile")
    args = parser.parse_args()

    if args.validate_package:
        candidate_report = verify_candidate_package(
            args.validate_package, source_root=args.source_root,
            run_functional=args.run_functional,
        )
        print(json.dumps(candidate_report, indent=2))
        return 0 if candidate_report["ok"] else 1

    report = verify_release_gate(
        project_root=args.project_root,
        strict_git=args.strict_git,
        strict=args.strict,
        candidate=args.candidate,
        allow_unreleased=args.allow_unreleased,
        release_directory=args.release_dir,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("=" * 60)
        mode_str = "STRICT CANDIDATE GATE" if report.get("strict") else "DIAGNOSTIC AUDIT"
        print(f"RELEASE GATE {mode_str} — v{report.get('version', 'N/A')}")
        print("=" * 60)
        print(f"Status: {'[PASSED]' if report['ok'] else '[FAILED]'}\n")
        if report.get("candidate_package"):
            print(f"Candidate Package: {report['candidate_package']}\n")
        if report["checks_passed"]:
            print("Passed Checks:")
            for check in report["checks_passed"]:
                print(f"  [OK] {check}")
            print()
        if report["warnings"]:
            print("Warnings:")
            for warn in report["warnings"]:
                print(f"  [WARN] {warn}")
            print()
        if report["errors"]:
            print("Errors:")
            for err in report["errors"]:
                print(f"  [ERR] {err}")
            print()

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
