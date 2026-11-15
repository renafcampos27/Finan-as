"""Short-lived settings snapshots for one synchronous GUI refresh."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


_settings_reads: ContextVar[dict[tuple[int, str, str], Any] | None] = ContextVar(
    "gui_settings_reads", default=None
)


@contextmanager
def settings_read_scope() -> Iterator[None]:
    """Share nested reads, then discard them even if a refresh raises."""
    if _settings_reads.get() is not None:
        yield
        return
    token = _settings_reads.set({})
    try:
        yield
    finally:
        _settings_reads.reset(token)


def read_setting(repo: Any, key: str, default: str = "") -> Any:
    reads = _settings_reads.get()
    if reads is None:
        return repo.setting(key, default)
    cache_key = (id(repo), key, default)
    if cache_key not in reads:
        reads[cache_key] = repo.setting(key, default)
    return reads[cache_key]


def invalidate_setting_read(repo: Any, key: str) -> None:
    """A write inside the operation must be visible to its following reads."""
    reads = _settings_reads.get()
    if reads is not None:
        for cache_key in tuple(reads):
            if cache_key[:2] == (id(repo), key):
                del reads[cache_key]
