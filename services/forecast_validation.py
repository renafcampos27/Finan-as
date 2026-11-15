"""Walk-forward validation helpers for adaptive financial forecasts.

The correction used for the next forecast may learn from every completed
historical month.  Reported accuracy, however, must only use months that were
predicted with a correction learned *before* their real result was known.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from utils.helpers import safe_float


DEFAULT_OPTIMISTIC_MULTIPLIER = 0.8
DEFAULT_PESSIMISTIC_MULTIPLIER = 1.2
RESIDUAL_QUANTILES = (0.2, 0.8)
RESIDUAL_NOMINAL_COVERAGE_PCT = 60.0


def _finite(value: Any) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("A previsão exige valores numéricos finitos.") from exc
    if not math.isfinite(result):
        raise ValueError("A previsão exige valores numéricos finitos.")
    return result


def measure_interval_coverage(
    rows: Iterable[dict[str, Any]],
    *,
    actual_key: str = "actual",
    low_key: str = "low",
    high_key: str = "high",
    prefer_validation: bool = True,
    nominal_coverage_pct: float = 80.0,
) -> dict[str, Any]:
    """Score point error, calibration and interval sharpness together.

    When walk-forward validation rows are available, only those genuinely
    unseen outcomes contribute.  Coverage remains visible, but a very wide
    interval can no longer obtain a perfect quality score merely by containing
    every real value.  The interval score is the Winkler score for the stated
    central coverage target.
    """

    target_coverage = max(50.0, min(99.0, _finite(nominal_coverage_pct)))
    alpha = max(0.01, (100.0 - target_coverage) / 100.0)
    prepared: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        if actual_key not in row:
            raise ValueError("A avaliação exige o valor observado de cada período.")
        low = _finite(row.get(low_key, 0.0))
        high = _finite(row.get(high_key, 0.0))
        if high < low:
            low, high = high, low
        actual = _finite(row[actual_key])
        predicted = _finite(row.get("predicted", row.get("predicted_raw", 0.0)))
        within_band = low <= actual <= high
        point_error_pct = (
            abs(predicted - actual) / actual * 100.0
            if actual > 0
            else 100.0
            if predicted > 0
            else 0.0
        )
        point_accuracy = max(0.0, 100.0 - min(100.0, point_error_pct))
        row.update(
            {
                "low": low,
                "high": high,
                "within_band": within_band,
                "band_status": ("Dentro da faixa" if within_band else "Fora da faixa"),
                "band_width": max(0.0, high - low),
                "is_interval_evaluation": False,
                "interval_hit_pct": 100.0 if within_band else 0.0,
                "error_pct": safe_float(row.get("error_pct"), point_error_pct),
                "error_accuracy_pct": safe_float(
                    row.get("error_accuracy_pct"), point_accuracy
                ),
            }
        )
        prepared.append(row)

    validation_indexes = [
        index for index, row in enumerate(prepared) if bool(row.get("is_validation"))
    ]
    evaluated_indexes = (
        validation_indexes
        if prefer_validation and validation_indexes
        else list(range(len(prepared)))
    )
    for index in evaluated_indexes:
        prepared[index]["is_interval_evaluation"] = True

    hits = 0
    lower_misses = 0
    upper_misses = 0
    width_percentages: list[float] = []
    interval_score_percentages: list[float] = []
    point_accuracies: list[float] = []
    for index in evaluated_indexes:
        row = prepared[index]
        actual = safe_float(row.get(actual_key))
        predicted = safe_float(
            row.get("predicted"),
            safe_float(row.get("predicted_raw")),
        )
        low = safe_float(row.get("low"))
        high = safe_float(row.get("high"))
        width = max(0.0, high - low)
        interval_score = width
        if actual < low:
            interval_score += 2.0 / alpha * (low - actual)
            lower_misses += 1
        elif actual > high:
            interval_score += 2.0 / alpha * (actual - high)
            upper_misses += 1
        else:
            hits += 1
        scale = max(1.0, abs(actual), abs(predicted))
        width_pct = width / scale * 100.0
        interval_score_pct = interval_score / scale * 100.0
        interval_quality = max(0.0, 100.0 - min(100.0, interval_score_pct))
        sharpness = max(0.0, 100.0 - min(100.0, width_pct))
        point_accuracy = safe_float(row.get("error_accuracy_pct"))
        row_quality = point_accuracy * 0.45 + interval_quality * 0.40 + sharpness * 0.15
        row.update(
            {
                "interval_score": interval_score,
                "interval_score_pct": interval_score_pct,
                "band_width_pct": width_pct,
                "forecast_quality_pct": row_quality,
                "accuracy_pct": row_quality,
            }
        )
        width_percentages.append(width_pct)
        interval_score_percentages.append(interval_score_pct)
        point_accuracies.append(point_accuracy)

    total = len(evaluated_indexes)
    misses = max(0, total - hits)
    coverage = hits / total * 100.0 if total else 0.0
    mean_width_pct = sum(width_percentages) / total if total else 0.0
    mean_interval_score_pct = sum(interval_score_percentages) / total if total else 0.0
    error_accuracy = sum(point_accuracies) / total if total else 0.0
    calibration_score = (
        max(0.0, 100.0 - max(0.0, target_coverage - coverage) * 2.5) if total else 0.0
    )
    sharpness_score = max(0.0, 100.0 - min(100.0, mean_width_pct)) if total else 0.0
    interval_quality_score = (
        max(0.0, 100.0 - min(100.0, mean_interval_score_pct)) if total else 0.0
    )
    forecast_quality = (
        error_accuracy * 0.45
        + interval_quality_score * 0.30
        + calibration_score * 0.15
        + sharpness_score * 0.10
        if total
        else 0.0
    )
    return {
        "rows": prepared,
        "interval_hits": hits,
        "interval_misses": misses,
        "interval_lower_misses": lower_misses,
        "interval_upper_misses": upper_misses,
        "interval_total": total,
        "interval_coverage_pct": coverage,
        "nominal_coverage_pct": target_coverage,
        "mean_band_width_pct": mean_width_pct,
        "mean_interval_score_pct": mean_interval_score_pct,
        "calibration_score": calibration_score,
        "sharpness_score": sharpness_score,
        "interval_quality_score": interval_quality_score,
        "error_accuracy_pct": error_accuracy,
        "forecast_quality_pct": forecast_quality,
        "accuracy_pct": forecast_quality,
        "accuracy_available": total > 0,
        "accuracy_method": "composite_interval_quality",
        "coverage_uses_validation": bool(validation_indexes) and prefer_validation,
    }


def percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(_finite(value) for value in values)
    if not ordered:
        raise ValueError("Um quantil exige pelo menos uma observação.")
    fraction = _finite(fraction)
    if not 0 <= fraction <= 1:
        raise ValueError("A fração do quantil deve estar entre zero e um.")
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, float(fraction))) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def weighted_correction(ratios: Iterable[float]) -> float:
    """Return a recent-weighted, outlier-resistant correction factor."""

    finite_values = [_finite(value) for value in ratios]
    cleaned = [max(0.55, min(1.8, value)) for value in finite_values if value > 0]
    if not cleaned:
        return 1.0
    weights = list(range(1, len(cleaned) + 1))
    correction = sum(ratio * weight for ratio, weight in zip(cleaned, weights)) / sum(
        weights
    )
    return max(0.7, min(1.5, correction))


def _confidence(validation_months: int) -> tuple[str, str]:
    if validation_months >= 6:
        return "high", "Aprendizagem forte"
    if validation_months >= 3:
        return "medium", "Aprendizagem estável"
    return "initial", "Aprendizagem em validação"


def walk_forward_calibration(
    rows: Iterable[dict[str, Any]],
    *,
    prediction_key: str = "predicted_raw",
    actual_key: str = "actual",
    minimum_training_months: int = 3,
    minimum_band_months: int = 3,
) -> dict[str, Any]:
    """Calibrate chronologically and evaluate only genuinely unseen months.

    Each validation row uses correction ratios from earlier rows only.  Its
    actual value is appended to the training history only after the prediction
    has been evaluated.  This prevents in-sample accuracy from being presented
    as if it were an observed forecasting result.
    """

    minimum_training = max(2, int(minimum_training_months or 3))
    minimum_band = max(2, int(minimum_band_months or 3))
    prepared: list[dict[str, Any]] = []
    historical_ratios: list[float] = []
    validation_errors: list[float] = []
    validation_biases: list[float] = []
    validation_residuals: list[float] = []
    interval_hits = 0

    for source in rows:
        row = dict(source)
        if prediction_key not in row or actual_key not in row:
            raise ValueError("A calibração exige a previsão e o valor observado de cada mês.")
        predicted_raw = _finite(row[prediction_key])
        actual = _finite(row[actual_key])
        trainable = predicted_raw > 0 and actual > 0
        evaluatable = predicted_raw > 0 or actual > 0
        row["predicted_raw"] = predicted_raw
        row["validation_correction_factor"] = 1.0
        row["is_validation"] = False
        row["validation_order"] = 0

        if evaluatable and len(historical_ratios) >= minimum_training:
            correction = weighted_correction(historical_ratios)
            predicted = predicted_raw * correction
            band_learned_for_row = len(validation_residuals) >= minimum_band
            row_optimistic = (
                max(
                    0.65,
                    min(0.98, percentile(validation_residuals, RESIDUAL_QUANTILES[0])),
                )
                if band_learned_for_row
                else DEFAULT_OPTIMISTIC_MULTIPLIER
            )
            row_pessimistic = (
                min(
                    1.65,
                    max(1.02, percentile(validation_residuals, RESIDUAL_QUANTILES[1])),
                )
                if band_learned_for_row
                else DEFAULT_PESSIMISTIC_MULTIPLIER
            )
            interval_low = min(
                predicted * row_optimistic,
                predicted * row_pessimistic,
            )
            interval_high = max(
                predicted * row_optimistic,
                predicted * row_pessimistic,
            )
            within_band = interval_low <= actual <= interval_high
            error_pct = (
                abs(predicted - actual) / actual * 100.0
                if actual > 0
                else 100.0
                if predicted > 0
                else 0.0
            )
            bias_pct = (
                (predicted - actual) / actual * 100.0
                if actual > 0
                else 100.0
                if predicted > 0
                else 0.0
            )
            residual = actual / predicted if predicted > 0 else 1.0
            row.update(
                {
                    "predicted": predicted,
                    "validation_correction_factor": correction,
                    "is_validation": True,
                    "validation_order": len(validation_errors) + 1,
                    "error_pct": error_pct,
                    "accuracy_pct": 100.0 if within_band else 0.0,
                    "error_accuracy_pct": max(
                        0.0,
                        100.0 - min(100.0, error_pct),
                    ),
                    "bias_pct": bias_pct,
                    "residual_ratio": residual,
                    "low": interval_low,
                    "high": interval_high,
                    "within_band": within_band,
                    "band_status": (
                        "Dentro da faixa" if within_band else "Fora da faixa"
                    ),
                    "band_learned_for_row": band_learned_for_row,
                    "band_sample_size": len(validation_residuals),
                    "residual_quantiles": RESIDUAL_QUANTILES,
                    "nominal_coverage_pct": RESIDUAL_NOMINAL_COVERAGE_PCT,
                    "band_method": "clamped_empirical_residual_quantiles" if band_learned_for_row else "initial_scenario_multipliers",
                    "is_interval_evaluation": True,
                }
            )
            interval_hits += int(within_band)
            validation_errors.append(error_pct)
            validation_biases.append(bias_pct)
            if predicted > 0:
                validation_residuals.append(residual)
        else:
            raw_error = (
                abs(predicted_raw - actual) / actual * 100.0
                if actual > 0
                else 100.0
                if predicted_raw > 0
                else 0.0
            )
            row.update(
                {
                    "predicted": predicted_raw,
                    "error_pct": raw_error,
                    "accuracy_pct": max(
                        0.0,
                        100.0 - min(100.0, raw_error),
                    ),
                    "error_accuracy_pct": max(
                        0.0,
                        100.0 - min(100.0, raw_error),
                    ),
                    "bias_pct": (
                        (predicted_raw - actual) / actual * 100.0
                        if actual > 0
                        else 100.0
                        if predicted_raw > 0
                        else 0.0
                    ),
                }
            )

        prepared.append(row)
        if trainable:
            historical_ratios.append(actual / predicted_raw)

    sample_months = len(historical_ratios)
    validation_months = len(validation_errors)
    enabled = sample_months >= minimum_training
    correction_factor = weighted_correction(historical_ratios) if enabled else 1.0
    accuracy_available = validation_months > 0
    mape = sum(validation_errors) / validation_months if accuracy_available else 0.0
    bias = sum(validation_biases) / validation_months if accuracy_available else 0.0
    band_learned = len(validation_residuals) >= minimum_band
    optimistic = (
        max(0.65, min(0.98, percentile(validation_residuals, RESIDUAL_QUANTILES[0])))
        if band_learned
        else DEFAULT_OPTIMISTIC_MULTIPLIER
    )
    pessimistic = (
        min(1.65, max(1.02, percentile(validation_residuals, RESIDUAL_QUANTILES[1])))
        if band_learned
        else DEFAULT_PESSIMISTIC_MULTIPLIER
    )
    confidence, confidence_label = _confidence(validation_months)
    interval_coverage = (
        interval_hits / validation_months * 100.0 if validation_months else 0.0
    )
    quality = measure_interval_coverage(
        prepared,
        prefer_validation=True,
        nominal_coverage_pct=RESIDUAL_NOMINAL_COVERAGE_PCT,
    )
    quality_rows = list(quality.get("rows") or prepared)
    forecast_quality = (
        safe_float(quality.get("forecast_quality_pct")) if accuracy_available else 0.0
    )

    return {
        "enabled": enabled,
        "sample_months": sample_months,
        "training_months": sample_months,
        "required_months": minimum_training,
        "validation_months": validation_months,
        "minimum_band_months": minimum_band,
        "band_learned": band_learned,
        "band_sample_size": len(validation_residuals),
        "residual_quantiles": RESIDUAL_QUANTILES,
        "nominal_coverage_pct": RESIDUAL_NOMINAL_COVERAGE_PCT,
        "band_method": "clamped_empirical_residual_quantiles" if band_learned else "initial_scenario_multipliers",
        "coverage_guaranteed": False,
        "band_note": (
            "Quantis empíricos P20–P80 (massa central nominal de 60%), com limites de cenário; "
            f"{len(validation_residuals)} resíduo(s) anterior(es). A amostra e os limites não garantem cobertura futura."
            if band_learned else "Faixa inicial de cenário; ainda não há amostra suficiente para quantis empíricos."
        ),
        "accuracy_available": accuracy_available,
        "correction_factor": correction_factor,
        "optimistic_multiplier": optimistic,
        "pessimistic_multiplier": pessimistic,
        "accuracy_pct": forecast_quality,
        "forecast_quality_pct": forecast_quality,
        "interval_coverage_pct": interval_coverage,
        "interval_hits": interval_hits,
        "interval_misses": max(0, validation_months - interval_hits),
        "interval_total": validation_months,
        "accuracy_method": "composite_interval_quality",
        "error_accuracy_pct": (
            max(0.0, 100.0 - min(100.0, mape)) if accuracy_available else 0.0
        ),
        "mean_band_width_pct": (
            safe_float(quality.get("mean_band_width_pct"))
            if accuracy_available
            else 0.0
        ),
        "mean_interval_score_pct": (
            safe_float(quality.get("mean_interval_score_pct"))
            if accuracy_available
            else 0.0
        ),
        "calibration_score": (
            safe_float(quality.get("calibration_score")) if accuracy_available else 0.0
        ),
        "sharpness_score": (
            safe_float(quality.get("sharpness_score")) if accuracy_available else 0.0
        ),
        "interval_quality_score": (
            safe_float(quality.get("interval_quality_score"))
            if accuracy_available
            else 0.0
        ),
        "mape_pct": mape,
        "bias_pct": bias,
        "confidence": confidence,
        "confidence_label": confidence_label,
        "rows": quality_rows,
        "validation_rows": [
            dict(row) for row in quality_rows if bool(row.get("is_validation"))
        ],
    }


__all__ = [
    "DEFAULT_OPTIMISTIC_MULTIPLIER",
    "DEFAULT_PESSIMISTIC_MULTIPLIER",
    "RESIDUAL_QUANTILES",
    "RESIDUAL_NOMINAL_COVERAGE_PCT",
    "measure_interval_coverage",
    "percentile",
    "walk_forward_calibration",
    "weighted_correction",
]
