"""Export an audited source snapshot without local history or personal state.

The destination must be new. Only its ``source`` directory belongs in Git;
``manifest.json`` is local review evidence. This targeted credential scan is
an additional check, not a guarantee that arbitrary source contains no secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path


ROOT_FILES = frozenset({
    ".gitattributes", ".gitignore", "LiquidGlass.spec", "main.py",
    "pyproject.toml", "pytest.ini", "requirements.txt", "requirements-dev.txt",
    "financas_liquid_glass_launcher.pyw", "run_liquid_glass_sem_consola.vbs",
    "run_windows_liquid_glass.bat", "run_windows_liquid_glass_debug.bat",
    "README.md", "COMO_INSTALAR_NOUTRO_PC.txt", "DATA_SAFETY.md",
    "RELEASE_NOTES.md", "RELEASE_NOTES_RESUME.md", "TEST_INFRA.md",
})
DIRECTORY_SUFFIXES = {
    "assets": frozenset({".svg", ".png", ".ico", ".wav", ".qss"}),
    "database": frozenset({".py"}),
    "domain": frozenset({".py"}),
    "motores_matematicos_simulacao": frozenset({".py"}),
    "packaging": frozenset({".ps1", ".bat"}),
    "scripts": frozenset({".py", ".ps1"}),
    "services": frozenset({".py"}),
    "tests": frozenset({".py"}),
    "ui": frozenset({".py"}),
    "utils": frozenset({".py"}),
    ".github": frozenset({".yaml", ".yml"}),
}
REQUIRED_FILES = frozenset({
    "main.py", "utils/version.py", "requirements.txt", "requirements-dev.txt",
    "LiquidGlass.spec", "scripts/build_windows.ps1",
})
SKIPPED_DIRECTORIES = frozenset({
    "__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".venv", "venv",
    "artifacts", "backups", "anexos", "logs", "build", "dist", ".agents",
})
MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024

# Exact, reviewed synthetic literals in redaction/fake-store regression tests.
# A new token, or the same token in a different file, still blocks the export.
SYNTHETIC_TOKEN_HASHES = {
    "tests/test_audit_d4_d5_e1_e2_defects.py": frozenset({
        "3c02576d6e6d4918349d9a8d9507497e95d134ce84f678ef5f184b37a784b2d6",
    }),
    "tests/test_phase5_security_and_ai_privacy.py": frozenset({
        "045ecd2d798cbff45656b78a3df0c07fe7894f93e4cdf77b3154490ebc8bb6da",
    }),
}
CREDENTIAL_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "OpenAI token": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{25,}"),
    "Google token": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
}


class SourceExportError(ValueError):
    """Publication preparation failed without exposing sensitive values."""


def _assert_regular(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise SourceExportError("Source links/reparse points are not allowed")
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
        raise SourceExportError("Special source files are not allowed")
    return metadata


def _walk(directory: Path, suffixes: frozenset[str]) -> list[Path]:
    _assert_regular(directory)
    candidates = []
    for child in sorted(directory.iterdir()):
        if child.name in SKIPPED_DIRECTORIES:
            continue
        metadata = _assert_regular(child)
        if stat.S_ISDIR(metadata.st_mode):
            candidates.extend(_walk(child, suffixes))
        elif child.suffix.lower() in suffixes:
            candidates.append(child)
    return candidates


def _check_credentials(relative: str, data: bytes) -> None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        if Path(relative).suffix.lower() not in {".png", ".ico", ".wav"}:
            raise SourceExportError(f"Non-UTF-8 source: {relative}") from None
        return
    for kind, pattern in CREDENTIAL_PATTERNS.items():
        for match in pattern.finditer(text):
            token_hash = hashlib.sha256(match.group().encode()).hexdigest()
            if token_hash in SYNTHETIC_TOKEN_HASHES.get(relative, ()):
                continue
            line = text.count("\n", 0, match.start()) + 1
            raise SourceExportError(f"Possible {kind}: {relative}:{line}")


def collect_source(source_root: Path) -> dict[str, bytes]:
    """Read only allowed files and reject links or credential-shaped literals."""
    source_root = source_root.absolute()
    _assert_regular(source_root)
    candidates = []
    for name in sorted(ROOT_FILES):
        path = source_root / name
        if path.exists() or path.is_symlink():
            candidates.append(path)
    for name, suffixes in DIRECTORY_SUFFIXES.items():
        path = source_root / name
        if path.exists() or path.is_symlink():
            candidates.extend(_walk(path, suffixes))
    payload = {}
    for path in sorted(candidates):
        relative = path.relative_to(source_root).as_posix()
        before = _assert_regular(path)
        if not stat.S_ISREG(before.st_mode):
            raise SourceExportError(f"Expected regular source file: {relative}")
        if before.st_size > MAX_SOURCE_FILE_BYTES:
            raise SourceExportError(f"Source file exceeds review limit: {relative}")
        data = path.read_bytes()
        after = _assert_regular(path)
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino
        ) or len(data) != before.st_size:
            raise SourceExportError(f"Source changed during export: {relative}")
        _check_credentials(relative, data)
        payload[relative] = data
    missing = REQUIRED_FILES - payload.keys()
    if missing:
        raise SourceExportError("Required source missing: " + ", ".join(sorted(missing)))
    return payload


def prepare_source(source_root: Path, output_root: Path) -> dict:
    """Create a new source snapshot and adjacent hash manifest; never overwrite."""
    source_root = source_root.absolute()
    output_root = output_root.absolute()
    # Resolve parent aliases before allowing writes. An in-project export may
    # only live in artifacts, which the allowlist never traverses.
    source_resolved = source_root.resolve()
    output_resolved = output_root.resolve()
    if source_resolved.is_relative_to(output_resolved) or (
        output_resolved.is_relative_to(source_resolved)
        and not output_resolved.is_relative_to(source_resolved / "artifacts")
    ):
        raise SourceExportError("Destination must be outside source, or under artifacts")
    if output_root.exists() or output_root.is_symlink():
        raise SourceExportError("Destination already exists; choose a new directory")
    payload = collect_source(source_root)
    manifest = {
        "format": 1,
        "purpose": "curated-public-source-export",
        "history_included": False,
        "credential_scan": "known-provider-patterns-and-private-key-markers",
        "reviewed_synthetic_token_exceptions": sorted(SYNTHETIC_TOKEN_HASHES),
        "files": [
            {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(payload.items())
        ],
    }
    output_root.mkdir(parents=True, exist_ok=False)
    source_output = output_root / "source"
    source_output.mkdir()
    for name, data in payload.items():
        target = source_output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
    with (output_root / "manifest.json").open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        manifest = prepare_source(arguments.source, arguments.output)
    except (OSError, SourceExportError) as exc:
        parser.exit(1, f"Source export blocked: {exc}\n")
    print(f"Prepared {len(manifest['files'])} source files; review manifest.json before publication.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
