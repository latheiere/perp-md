from __future__ import annotations

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


def protocol_interval() -> FundingIntervalV1:
    return FundingIntervalV1(FundingIntervalKind.PROTOCOL_SCHEDULE)


def utc_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000, timezone.utc)


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
