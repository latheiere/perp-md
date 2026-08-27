from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from cdm import (
    FundingIntervalV1,
    FundingRateKind,
    FundingSampleV1,
    NotionalDenomination,
    OpenInterestMeasure,
    OpenInterestValueV1,
)

from perp_md.errors import InvalidInstrument, InvalidResponse


class ContractDirection(StrEnum):
    LINEAR = "linear"
    INVERSE = "inverse"


class NativeUnit(StrEnum):
    CONTRACTS = "contracts"
    BASE = "base"
    QUOTE = "quote"


class ValuationMethod(StrEnum):
    VENUE_REPORTED = "venue_reported"
    CONTRACT_VALUE = "contract_value"
    MARK_PRICE = "mark_price"
    CURRENT_MARK = "current_mark"


class ObservationTimeKind(StrEnum):
    SOURCE = "source"
    RETRIEVED = "retrieved"


@dataclass(frozen=True, slots=True)
class Instrument:
    venue: str
    symbol: str
    market_type: str = "perpetual"
    base_currency: str | None = None
    quote_currency: str | None = None
    settlement_currency: str | None = None
    contract_direction: ContractDirection | None = None
    contract_multiplier: float | None = None
    product: str | None = None
    pair_symbol: str | None = None

    def __post_init__(self) -> None:
        venue = self.venue.strip().upper()
        symbol = self.symbol.strip()
        if not venue:
            raise InvalidInstrument("venue is required")
        if not symbol:
            raise InvalidInstrument("venue-native symbol is required")
        object.__setattr__(self, "venue", venue)
        object.__setattr__(self, "symbol", symbol)
        if self.contract_multiplier is not None:
            try:
                value = float(self.contract_multiplier)
            except (TypeError, ValueError) as exc:
                raise InvalidInstrument(
                    "contract_multiplier must be finite and positive"
                ) from exc
            if not math.isfinite(value) or value <= 0:
                raise InvalidInstrument(
                    "contract_multiplier must be finite and positive"
                )
            object.__setattr__(self, "contract_multiplier", value)


@dataclass(frozen=True, slots=True)
class HistoryRange:
    start_ms: int | None = None
    end_ms: int | None = None

    def __post_init__(self) -> None:
        if self.start_ms is not None and self.start_ms < 0:
            raise ValueError("start_ms must not be negative")
        if self.end_ms is not None and self.end_ms < 0:
            raise ValueError("end_ms must not be negative")
        if self.start_ms is None and self.end_ms is not None:
            raise ValueError("end_ms requires start_ms")
        if (
            self.start_ms is not None
            and self.end_ms is not None
            and self.start_ms > self.end_ms
        ):
            raise ValueError("start_ms must not exceed end_ms")


@dataclass(frozen=True, slots=True)
class OpenInterestObservation:
    timestamp_ms: int
    value_usd: float
    native_value: float | None = None
    native_unit: NativeUnit | None = None
    mark_price: float | None = None
    valuation: ValuationMethod = ValuationMethod.VENUE_REPORTED
    base_quantity: OpenInterestValueV1 | None = None
    timestamp_kind: ObservationTimeKind = ObservationTimeKind.SOURCE

    def __post_init__(self) -> None:
        value = float(self.value_usd)
        if not math.isfinite(value) or value < 0:
            raise InvalidResponse("open interest must be finite and non-negative")
        object.__setattr__(self, "value_usd", value)
        if self.native_value is not None:
            native = float(self.native_value)
            if not math.isfinite(native) or native < 0:
                raise InvalidResponse(
                    "native open interest must be finite and non-negative"
                )
            object.__setattr__(self, "native_value", native)
        if self.mark_price is not None:
            mark = float(self.mark_price)
            if not math.isfinite(mark) or mark <= 0:
                raise InvalidResponse("mark price must be finite and positive")
            object.__setattr__(self, "mark_price", mark)
        if self.base_quantity is not None and not isinstance(
            self.base_quantity, OpenInterestValueV1
        ):
            raise InvalidResponse(
                "base quantity must use the CDM open-interest contract"
            )
        if not isinstance(self.timestamp_kind, ObservationTimeKind):
            raise InvalidResponse("open-interest timestamp kind is invalid")

    @property
    def notional(self) -> OpenInterestValueV1:
        """Return the legacy reporting-notional output through the CDM contract."""

        return OpenInterestValueV1(
            Decimal(str(self.value_usd)),
            OpenInterestMeasure.NOTIONAL,
            NotionalDenomination.REPORTING,
        )


@dataclass(frozen=True, slots=True)
class HistoryIssue:
    code: str
    message: str
    retryable: bool = True


@dataclass(frozen=True, slots=True)
class OpenInterestResult:
    current: OpenInterestObservation
    history: tuple[OpenInterestObservation, ...] = ()
    history_issue: HistoryIssue | None = None


@dataclass(frozen=True, slots=True)
class OpenInterestCapabilities:
    current: bool
    history: bool
    history_interval_seconds: int | None = None
    max_history_days: int | None = None
    required_metadata: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderFundingEvidence:
    """Acquisition evidence that is intentionally outside the CDM economics."""

    source_observation: str
    retrieved_at: datetime
    source_value: Decimal | None = None
    mark_price: Decimal | None = None

    def __post_init__(self) -> None:
        if (
            not self.source_observation
            or self.source_observation != self.source_observation.strip()
        ):
            raise InvalidResponse(
                "funding evidence requires a stable source observation"
            )
        if (
            self.retrieved_at.tzinfo is None
            or self.retrieved_at.utcoffset()
            != timezone.utc.utcoffset(self.retrieved_at)
        ):
            raise InvalidResponse("funding retrieval time must be UTC-aware")
        if self.source_value is not None and (
            not isinstance(self.source_value, Decimal)
            or not self.source_value.is_finite()
        ):
            raise InvalidResponse("funding source value must be a finite Decimal")
        if self.mark_price is not None:
            if (
                not isinstance(self.mark_price, Decimal)
                or not self.mark_price.is_finite()
                or self.mark_price <= 0
            ):
                raise InvalidResponse(
                    "funding evidence mark price must be a positive finite Decimal"
                )


@dataclass(frozen=True, slots=True)
class FundingObservation:
    sample: FundingSampleV1
    provider_evidence: ProviderFundingEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.sample, FundingSampleV1):
            raise InvalidResponse("funding sample must use the CDM funding contract")
        if not isinstance(self.provider_evidence, ProviderFundingEvidence):
            raise InvalidResponse("funding observation requires provider evidence")

    @property
    def timestamp_ms(self) -> int:
        point = (
            self.sample.effective_at
            if self.sample.kind in (FundingRateKind.NEXT, FundingRateKind.SETTLED)
            else self.sample.observed_at or self.provider_evidence.retrieved_at
        )
        assert point is not None
        return int(point.timestamp() * 1_000)

    @property
    def rate(self) -> float:
        return float(self.sample.rate)

    @property
    def kind(self) -> FundingRateKind:
        return self.sample.kind

    @property
    def interval(self) -> FundingIntervalV1:
        return self.sample.interval


@dataclass(frozen=True, slots=True)
class FundingResult:
    current: FundingObservation
    history: tuple[FundingObservation, ...] = ()
    history_issue: HistoryIssue | None = None


@dataclass(frozen=True, slots=True)
class FundingCapabilities:
    current: bool
    current_kinds: tuple[FundingRateKind, ...]
    history: bool
    history_requires_start: bool = False
    declared_interval: FundingIntervalV1 | None = None
    required_metadata: tuple[str, ...] = ()
    runtime_conditional: bool = False
