from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from cdm import FundingIntervalV1, FundingRateKind, TemporalMode

from perp_md.adapters.ccxt import CcxtAdapter
from perp_md.capabilities import CCXT_FUNDING_FEATURE, CCXT_FUNDING_HISTORY_FEATURE
from perp_md.errors import (
    AdapterUnavailable,
    DataUnavailable,
    InvalidResponse,
    PerpMdError,
    RequestError,
)
from perp_md.funding_values import (
    explicit_interval,
    funding_observation,
    unspecified_interval,
)
from perp_md.models import (
    FundingCapabilities,
    FundingObservation,
    FundingResult,
    HistoryIssue,
    HistoryRange,
    Instrument,
)

CCXT_FUNDING_HISTORY_LIMIT = 100


@dataclass
class CcxtFundingAdapter(CcxtAdapter):
    """Optional-provider funding adapter with runtime-declared availability."""

    def capabilities(self, instrument: Instrument) -> FundingCapabilities:
        return FundingCapabilities(
            True,
            (FundingRateKind.INDICATIVE, FundingRateKind.SETTLED),
            True,
            runtime_conditional=True,
        )

    async def runtime_features(self, instrument: Instrument) -> frozenset[str]:
        try:
            exchange, owned = self._runtime_exchange(instrument)
        except AdapterUnavailable:
            return frozenset()
        try:
            features: set[str] = set()
            declared = getattr(exchange, "has", None)
            supported = declared if isinstance(declared, dict) else {}
            if bool(supported.get("fetchFundingRate")):
                features.add(CCXT_FUNDING_FEATURE)
            if bool(supported.get("fetchFundingRateHistory")):
                features.add(CCXT_FUNDING_HISTORY_FEATURE)
            return frozenset(features)
        finally:
            if owned:
                await exchange.close()

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> FundingResult:
        try:
            exchange, symbol = await self._market(instrument)
            has_current = bool(exchange.has.get("fetchFundingRate"))
            has_history = bool(exchange.has.get("fetchFundingRateHistory"))
            if not has_current and not has_history:
                raise DataUnavailable(
                    "optional provider exposes neither current nor historical funding"
                )

            current_payload: dict[str, Any] = {}
            if has_current:
                candidate = await exchange.fetch_funding_rate(symbol)
                if not isinstance(candidate, dict):
                    raise InvalidResponse(
                        "optional provider returned an invalid current funding response"
                    )
                current_payload = candidate

            history_payload: Any = []
            history_issue: HistoryIssue | None = None
            if has_history and (include_history or not has_current):
                try:
                    history_payload = await exchange.fetch_funding_rate_history(
                        symbol,
                        since=history.start_ms if history else None,
                        limit=CCXT_FUNDING_HISTORY_LIMIT if include_history else 1,
                    )
                except Exception as exc:
                    if not has_current:
                        raise
                    history_issue = HistoryIssue(
                        "history_unavailable", self._summary(exc)
                    )

            retrieved_at_ms = int(time.time() * 1_000)
            history_rows = self._history_rows(
                history_payload,
                history,
                retrieved_at_ms=retrieved_at_ms,
            )
            if current_payload.get("fundingRate") is not None:
                timestamp = current_payload.get("timestamp")
                current = self._observation(
                    int(timestamp) if timestamp is not None else None,
                    current_payload["fundingRate"],
                    FundingRateKind.INDICATIVE,
                    TemporalMode.CURRENT,
                    self._reported_interval(current_payload),
                    retrieved_at_ms=retrieved_at_ms,
                )
            elif history_rows:
                latest = history_rows[-1]
                current = self._observation(
                    latest.timestamp_ms,
                    latest.sample.rate,
                    FundingRateKind.SETTLED,
                    TemporalMode.SETTLED,
                    latest.interval,
                    retrieved_at_ms=retrieved_at_ms,
                )
            else:
                raise DataUnavailable("optional provider returned no funding rate")
            return FundingResult(
                current,
                history_rows if include_history else (),
                history_issue,
            )
        except PerpMdError:
            raise
        except Exception as exc:
            raise RequestError("optional provider funding request failed") from exc

    @classmethod
    def _history_rows(
        cls,
        payload: Any,
        requested: HistoryRange | None,
        *,
        retrieved_at_ms: int,
    ) -> tuple[FundingObservation, ...]:
        if not isinstance(payload, list):
            raise InvalidResponse(
                "optional provider returned an invalid settled funding history"
            )
        rows: dict[int, FundingObservation] = {}
        for source in payload:
            if not isinstance(source, dict):
                raise InvalidResponse(
                    "optional provider returned an invalid settled funding history row"
                )
            if source.get("timestamp") is None or source.get("fundingRate") is None:
                raise InvalidResponse(
                    "optional provider omitted a settled funding field"
                )
            timestamp = int(source["timestamp"])
            if requested is not None:
                if requested.start_ms is not None and timestamp < requested.start_ms:
                    continue
                if requested.end_ms is not None and timestamp > requested.end_ms:
                    continue
            point = cls._observation(
                timestamp,
                source["fundingRate"],
                FundingRateKind.SETTLED,
                TemporalMode.HISTORICAL,
                cls._reported_interval(source),
                retrieved_at_ms=retrieved_at_ms,
            )
            existing = rows.get(timestamp)
            if existing is not None and existing.rate != point.rate:
                raise InvalidResponse(
                    "optional provider returned conflicting settled funding rows"
                )
            rows[timestamp] = point
        return tuple(rows[key] for key in sorted(rows))

    @staticmethod
    def _observation(
        timestamp: int | None,
        rate: Any,
        kind: FundingRateKind,
        temporal_mode: TemporalMode,
        interval: FundingIntervalV1,
        *,
        retrieved_at_ms: int,
    ) -> FundingObservation:
        return funding_observation(
            source_time_ms=timestamp,
            retrieved_at_ms=retrieved_at_ms,
            rate=rate,
            kind=kind,
            temporal_mode=temporal_mode,
            interval=interval,
            source_observation=(
                "funding.indicative_rate"
                if kind is FundingRateKind.INDICATIVE
                else "funding.settled_rate"
            ),
            source_value=rate,
        )

    @staticmethod
    def _reported_interval(payload: dict[str, Any]) -> FundingIntervalV1:
        raw = payload.get("interval")
        if raw in (None, ""):
            raw = payload.get("fundingInterval")
        duration = _duration_seconds(raw) if raw not in (None, "") else None
        if duration is None:
            info = payload.get("info")
            minutes = (
                info.get("funding_interval_minutes")
                if isinstance(info, dict)
                else None
            )
            if minutes not in (None, ""):
                duration = _explicit_unit_duration(minutes, 60)
        return (
            explicit_interval(duration)
            if duration is not None
            else unspecified_interval()
        )


def _duration_seconds(value: Any) -> int | None:
    if isinstance(value, bool):
        raise InvalidResponse("optional provider returned an invalid funding interval")
    if isinstance(value, (int, float)):
        # The optional abstraction does not specify a unit for numeric interval
        # values, so retaining it as unspecified is more accurate than guessing.
        return None
    text = str(value).strip().lower()
    suffixes = {
        "ms": 0.001,
        "s": 1,
        "m": 60,
        "h": 3_600,
        "d": 86_400,
    }
    for suffix, multiplier in suffixes.items():
        if text.endswith(suffix):
            magnitude = text[: -len(suffix)]
            try:
                parsed = float(magnitude)
            except ValueError as exc:
                raise InvalidResponse(
                    "optional provider returned an invalid funding interval"
                ) from exc
            duration = parsed * multiplier
            if duration <= 0 or not duration.is_integer():
                raise InvalidResponse(
                    "optional provider returned an invalid funding interval"
                )
            return int(duration)
    return None


def _explicit_unit_duration(value: Any, multiplier: int) -> int:
    if isinstance(value, bool):
        raise InvalidResponse("optional provider returned an invalid funding interval")
    try:
        duration = float(value) * multiplier
    except (TypeError, ValueError) as exc:
        raise InvalidResponse(
            "optional provider returned an invalid funding interval"
        ) from exc
    if duration <= 0 or not duration.is_integer():
        raise InvalidResponse("optional provider returned an invalid funding interval")
    return int(duration)
