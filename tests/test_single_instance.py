from __future__ import annotations

from pathlib import Path

import main


def test_only_one_instance_can_hold_the_profile_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_path = tmp_path / "financas.lock"
    monkeypatch.setattr(main, "instance_lock_path", lambda: lock_path)

    first = main.acquire_single_instance_lock()
    second = main.acquire_single_instance_lock()

    assert first is not None
    assert second is None
    first.unlock()
    third = main.acquire_single_instance_lock()
    assert third is not None
    third.unlock()
