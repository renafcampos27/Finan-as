"""Structured progress events for the protected application startup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from utils.app_logging import log_exception


@dataclass(slots=True, frozen=True)
class StartupProgress:
    percent: int
    stage: str
    detail: str = ""
    current: int = 0
    total: int = 0


StartupProgressCallback = Callable[[StartupProgress], None]


def scaled_percent(
    start: int,
    end: int,
    current: int,
    total: int,
) -> int:
    """Map a real item/page counter into one monotonic startup phase."""

    low = min(int(start), int(end))
    high = max(int(start), int(end))
    if total <= 0:
        return low
    ratio = min(1.0, max(0.0, float(current) / float(total)))
    return int(round(low + (high - low) * ratio))


def report_startup_progress(
    callback: StartupProgressCallback | None,
    percent: int,
    stage: str,
    detail: str = "",
    *,
    current: int = 0,
    total: int = 0,
) -> None:
    """Send a safe, clamped progress event without risking stored data."""

    if callback is None:
        return
    event = StartupProgress(
        percent=min(100, max(0, int(percent))),
        stage=str(stage or "A preparar a aplicação"),
        detail=str(detail or ""),
        current=max(0, int(current)),
        total=max(0, int(total)),
    )
    try:
        callback(event)
    except Exception as exc:
        # A rendering issue must never turn a valid data migration into a
        # failed migration or trigger an unnecessary rollback.
        log_exception("Failed to update the startup progress window", exc)
