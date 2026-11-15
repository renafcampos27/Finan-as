from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from services.ai import GeminiClient
from services.backup import BackupService


class _StreamingResponse:
    status = 200

    def __init__(self, payload: bytes, *, announced_size: int | None = None) -> None:
        self._payload = payload
        self._offset = 0
        self._announced_size = announced_size
        self.read_calls = 0

    def getheader(self, name: str) -> str | None:
        if name.casefold() == "content-length" and self._announced_size is not None:
            return str(self._announced_size)
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if self._offset >= len(self._payload):
            return b""
        end = len(self._payload) if size < 0 else self._offset + size
        chunk = self._payload[self._offset : end]
        self._offset += len(chunk)
        return chunk


class _Connection:
    sock = None


def _client(limit: int) -> GeminiClient:
    return GeminiClient(
        "secret",
        "gemini-test",
        max_attempts=1,
        max_response_bytes=limit,
    )


def test_ai_response_limit_rejects_an_oversized_announced_body_before_reading() -> None:
    response = _StreamingResponse(b"{}", announced_size=65)

    with pytest.raises(RuntimeError, match="limite seguro"):
        _client(64)._read_response(
            response,  # type: ignore[arg-type]
            _Connection(),  # type: ignore[arg-type]
            time.monotonic() + 2,
        )

    assert response.read_calls == 0


def test_ai_response_limit_catches_streams_without_content_length() -> None:
    response = _StreamingResponse(b"x" * 65)

    with pytest.raises(RuntimeError, match="limite seguro"):
        _client(64)._read_response(
            response,  # type: ignore[arg-type]
            _Connection(),  # type: ignore[arg-type]
            time.monotonic() + 2,
        )

    assert response.read_calls <= 2


def test_ai_response_at_the_exact_limit_is_accepted() -> None:
    response = _StreamingResponse(b"x" * 64)

    body = _client(64)._read_response(
        response,  # type: ignore[arg-type]
        _Connection(),  # type: ignore[arg-type]
        time.monotonic() + 2,
    )

    assert body == b"x" * 64


def test_durable_replace_syncs_the_file_before_atomic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".backup.creating"
    target = tmp_path / "backup.zip"
    source.write_bytes(b"new backup")
    target.write_bytes(b"old backup")
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def recording_replace(old: os.PathLike[str], new: os.PathLike[str]) -> None:
        events.append("replace")
        real_replace(old, new)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)

    BackupService._durable_replace(source, target)

    assert events[:2] == ["fsync", "replace"]
    assert target.read_bytes() == b"new backup"
    assert not source.exists()


def test_failed_disk_sync_never_replaces_the_previous_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".backup.creating"
    target = tmp_path / "backup.zip"
    source.write_bytes(b"new backup")
    target.write_bytes(b"known good backup")

    def fail_sync(_descriptor: int) -> None:
        raise OSError("simulated disk flush failure")

    monkeypatch.setattr(os, "fsync", fail_sync)

    with pytest.raises(OSError, match="disk flush failure"):
        BackupService._durable_replace(source, target)

    assert target.read_bytes() == b"known good backup"
    assert source.read_bytes() == b"new backup"
