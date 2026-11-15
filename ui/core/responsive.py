"""Shared responsive rules for pages and dashboard layouts."""

from __future__ import annotations


class ResponsivePolicy:
    """Single source of truth for the application's main width breakpoints."""

    DASHBOARD_TWO_COLUMNS = 760
    DASHBOARD_THREE_COLUMNS = 1180
    METRICS_TWO_COLUMNS = 430
    METRICS_THREE_COLUMNS = 780
    ULTRAWIDE = 1920

    @staticmethod
    def grid_columns(
        width: int,
        max_columns: int,
        min_column_width: int,
        spacing: int = 0,
    ) -> int:
        safe_width = max(0, int(width))
        safe_maximum = max(1, int(max_columns))
        safe_minimum = max(1, int(min_column_width))
        safe_spacing = max(0, int(spacing))
        # Include the real space between columns.  Ignoring it made the last
        # card too narrow exactly around the responsive breakpoints.
        for columns in range(safe_maximum, 0, -1):
            required = columns * safe_minimum + (columns - 1) * safe_spacing
            if safe_width >= required:
                return columns
        return 1

    @classmethod
    def dashboard_columns(cls, width: int) -> int:
        safe_width = max(0, int(width))
        if safe_width >= cls.DASHBOARD_THREE_COLUMNS:
            return 3
        if safe_width >= cls.DASHBOARD_TWO_COLUMNS:
            return 2
        return 1

    @classmethod
    def metric_columns(cls, width: int) -> int:
        safe_width = max(0, int(width))
        if safe_width >= cls.METRICS_THREE_COLUMNS:
            return 3
        if safe_width >= cls.METRICS_TWO_COLUMNS:
            return 2
        return 1
