from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from cdm import (
    DataPointDefinitionV1,
    DataPointKind,
    DerivationKind,
    DerivationStepV1,
    FundingIntervalKind,
    FundingIntervalV1,
    FundingRateKind,
    FundingSampleV1,
    MeasurementLineageV1,
    MeasurementUnit,
    TemporalMode,
)

from perp_md.errors import InvalidResponse
from perp_md.models import FundingObservation, ProviderFundingEvidence

_FUNDING_BOUNDARY_JITTER_TOLERANCE_MS = 5


def funding_observation(
    *,
    source_time_ms: int | None,
    retrieved_at_ms: int,
    rate: Any,
    kind: FundingRateKind,
    temporal_mode: TemporalMode,
    interval: FundingIntervalV1,
    source_observation: str,
    source_value: Any | None = None,
    mark_price: Any | None = None,
    conversion_method: str | None = None,
) -> FundingObservation:
    output_kind = {
        FundingRateKind.INDICATIVE: DataPointKind.FUNDING_INDICATIVE_RATE,
        FundingRateKind.NEXT: DataPointKind.FUNDING_NEXT_RATE,
        FundingRateKind.SETTLED: DataPointKind.FUNDING_SETTLED_RATE,
    }[kind]
    steps = [DerivationStepV1(DerivationKind.NATIVE_REPORTED)]
    if conversion_method is not None:
        steps.append(
            DerivationStepV1(
                DerivationKind.PROVIDER_FORMULA,
                conversion_method,
            )
        )
    source_time = utc_ms(source_time_ms) if source_time_ms is not None else None
    sample = FundingSampleV1(
        rate=_decimal(rate),
        kind=kind,
        observed_at=source_time if kind is FundingRateKind.INDICATIVE else None,
        effective_at=(
            source_time
            if kind in (FundingRateKind.NEXT, FundingRateKind.SETTLED)
            else None
        ),
        interval=interval,
        lineage=MeasurementLineageV1(
            output=DataPointDefinitionV1(
                kind=output_kind,
                temporal_mode=temporal_mode,
                unit=MeasurementUnit.RATE_FRACTION,
            ),
            steps=tuple(steps),
        ),
    )
    return FundingObservation(
        sample,
        ProviderFundingEvidence(
            source_observation=source_observation,
            retrieved_at=utc_ms(retrieved_at_ms),
            source_value=_decimal(source_value) if source_value is not None else None,
            mark_price=_decimal(mark_price) if mark_price is not None else None,
        ),
    )


def unspecified_interval() -> FundingIntervalV1:
    return FundingIntervalV1(FundingIntervalKind.UNSPECIFIED)


def explicit_interval(duration_seconds: int) -> FundingIntervalV1:
    return FundingIntervalV1(
        FundingIntervalKind.EXPLICIT_DURATION,
        duration_seconds=duration_seconds,
    )


def observed_interval(start_ms: int, end_ms: int) -> FundingIntervalV1:
    return FundingIntervalV1(
        FundingIntervalKind.OBSERVED_WINDOW,
        duration_seconds=funding_window_duration_seconds(start_ms, end_ms),
        window_start=utc_ms(start_ms),
    )


def funding_window_duration_seconds(start_ms: int, end_ms: int) -> int:
    """Return proven whole-second duration without altering source timestamps."""

    duration_ms = end_ms - start_ms
    if duration_ms <= 0:
        raise InvalidResponse(
            "funding interval boundaries do not form an unambiguous positive "
            "whole-second window"
        )
    if duration_ms % 1_000 == 0:
        return duration_ms // 1_000

    normalized_start_ms = _nominal_whole_second_boundary(start_ms)
    normalized_end_ms = _nominal_whole_second_boundary(end_ms)
    normalized_duration_ms = normalized_end_ms - normalized_start_ms
    if normalized_duration_ms <= 0 or normalized_duration_ms % 1_000:
        raise InvalidResponse(
            "funding interval boundaries do not form an unambiguous positive "
            "whole-second window"
        )
    return normalized_duration_ms // 1_000


def preserve_observed_intervals(
    observations: tuple[FundingObservation, ...],
) -> tuple[FundingObservation, ...]:
    enriched: list[FundingObservation] = []
    for point in observations:
        if enriched and point.interval.kind is FundingIntervalKind.UNSPECIFIED:
            interval = observed_interval(enriched[-1].timestamp_ms, point.timestamp_ms)
            point = replace(point, sample=replace(point.sample, interval=interval))
        enriched.append(point)
    return tuple(enriched)


def protocol_interval() -> FundingIntervalV1:
    return FundingIntervalV1(FundingIntervalKind.PROTOCOL_SCHEDULE)


def utc_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000, timezone.utc)


def _nominal_whole_second_boundary(timestamp_ms: int) -> int:
    remainder_ms = timestamp_ms % 1_000
    if remainder_ms <= _FUNDING_BOUNDARY_JITTER_TOLERANCE_MS:
        return timestamp_ms - remainder_ms
    distance_to_next_second_ms = 1_000 - remainder_ms
    if distance_to_next_second_ms <= _FUNDING_BOUNDARY_JITTER_TOLERANCE_MS:
        return timestamp_ms + distance_to_next_second_ms
    raise InvalidResponse(
        "funding interval boundaries do not form an unambiguous positive "
        "whole-second window"
    )


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise InvalidResponse("provider returned a non-numeric value")
    try:
        parsed = Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise InvalidResponse("provider returned a non-numeric value") from exc
    if not parsed.is_finite():
        raise InvalidResponse("provider returned a non-finite value")
    return parsed
