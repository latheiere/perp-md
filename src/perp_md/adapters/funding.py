from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cdm import FundingIntervalKind, FundingIntervalV1, FundingRateKind, TemporalMode

from perp_md.adapters.native import HyperliquidAdapter
from perp_md.errors import (
    DataUnavailable,
    InvalidInstrument,
    InvalidResponse,
    PaginationError,
)
from perp_md.funding_values import (
    explicit_interval,
    funding_observation,
    funding_window_duration_seconds,
    preserve_observed_intervals,
    unspecified_interval,
)
from perp_md.models import (
    ContractDirection,
    FundingCapabilities,
    FundingObservation,
    FundingResult,
    HistoryIssue,
    HistoryRange,
    Instrument,
)
from perp_md.normalization import number
from perp_md.transport import JsonTransport

FUNDING_HISTORY_MAX_PAGES = 200
BINANCE_FUNDING_HISTORY_LIMIT = 1_000
BYBIT_FUNDING_HISTORY_LIMIT = 200
GATE_FUNDING_HISTORY_LIMIT = 1_000
KRAKEN_FUNDING_HISTORY_MAX_ROWS = 50_000
KRAKEN_FUNDING_INTERVAL_SECONDS = 3_600
HYPERLIQUID_FUNDING_INTERVAL_SECONDS = 3_600
HYPERLIQUID_FUNDING_INTERVAL_MS = HYPERLIQUID_FUNDING_INTERVAL_SECONDS * 1_000
KRAKEN_TICKERS_URL = "https://futures.kraken.com/derivatives/api/v3/tickers"
KRAKEN_FUNDING_HISTORY_URL = (
    "https://futures.kraken.com/derivatives/api/v4/historicalfundingrates"
)
OKX_PUBLIC_API_URL = "https://openapi.okx.com/api/v5/public"


@dataclass
class NativeFundingAdapter:
    transport: JsonTransport
    clock: Callable[[], float] = time.time

    async def close(self) -> None:
        return None

    @staticmethod
    def _issue(exc: Exception) -> HistoryIssue:
        detail = str(exc).strip()
        message = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        return HistoryIssue("history_unavailable", message)


class BinanceFundingAdapter(NativeFundingAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "BINANCE"

    def capabilities(self, instrument: Instrument) -> FundingCapabilities:
        return FundingCapabilities(
            True,
            (FundingRateKind.INDICATIVE,),
            True,
            required_metadata=("contract_direction",),
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> FundingResult:
        inverse = _inverse_product(instrument)
        prefix = (
            "https://dapi.binance.com/dapi/v1"
            if inverse
            else "https://fapi.binance.com/fapi/v1"
        )
        premium = await self.transport.get(
            f"{prefix}/premiumIndex", {"symbol": instrument.symbol}
        )
        if not isinstance(premium, dict):
            raise InvalidResponse("provider returned an invalid funding snapshot")
        if premium.get("lastFundingRate") is None or premium.get("time") is None:
            raise DataUnavailable(
                "provider omitted the indicative funding rate or source time"
            )
        interval = unspecified_interval()
        if not inverse:
            try:
                interval = await self._current_interval(instrument.symbol)
            except Exception:
                # Interval enrichment is independent from the valid rate snapshot.
                interval = unspecified_interval()
        if interval.kind is FundingIntervalKind.UNSPECIFIED:
            try:
                interval = await self._current_window_interval(
                    prefix,
                    instrument.symbol,
                    premium.get("nextFundingTime"),
                )
            except Exception:
                # Boundary enrichment is independent from the valid rate snapshot.
                interval = unspecified_interval()
        retrieved_at_ms = int(self.clock() * 1_000)
        current = _relative_observation(
            int(premium["time"]),
            premium["lastFundingRate"],
            FundingRateKind.INDICATIVE,
            TemporalMode.CURRENT,
            interval,
            retrieved_at_ms=retrieved_at_ms,
            source_observation="funding.indicative_rate",
        )
        if not include_history:
            return FundingResult(current)
        try:
            rows = await self._history(
                prefix, instrument.symbol, history, retrieved_at_ms
            )
            return FundingResult(current, rows)
        except Exception as exc:
            return FundingResult(current, history_issue=self._issue(exc))

    async def _current_interval(self, symbol: str) -> FundingIntervalV1:
        payload = await self.transport.get(
            "https://fapi.binance.com/fapi/v1/fundingInfo"
        )
        if not isinstance(payload, list):
            raise InvalidResponse("provider returned invalid funding interval metadata")
        matches = [
            row
            for row in payload
            if isinstance(row, dict) and row.get("symbol") == symbol
        ]
        if not matches:
            return unspecified_interval()
        if len(matches) != 1:
            raise InvalidResponse("provider returned ambiguous funding interval metadata")
        return _hour_interval(matches[0].get("fundingIntervalHours"))

    async def _current_window_interval(
        self, prefix: str, symbol: str, next_funding_time: Any
    ) -> FundingIntervalV1:
        if next_funding_time in (None, ""):
            return unspecified_interval()
        payload = await self.transport.get(
            f"{prefix}/fundingRate", {"symbol": symbol, "limit": 1}
        )
        if not isinstance(payload, list) or len(payload) != 1:
            raise InvalidResponse("provider returned invalid funding boundary metadata")
        row = payload[0]
        if not isinstance(row, dict) or row.get("fundingTime") is None:
            raise InvalidResponse("provider omitted a funding boundary")
        start_ms = _integer_timestamp_ms(row["fundingTime"])
        end_ms = _integer_timestamp_ms(next_funding_time)
        return explicit_interval(
            funding_window_duration_seconds(start_ms, end_ms)
        )

    async def _history(
        self,
        prefix: str,
        symbol: str,
        requested: HistoryRange | None,
        retrieved_at_ms: int,
    ) -> tuple[FundingObservation, ...]:
        base: dict[str, Any] = {
            "symbol": symbol,
            "limit": BINANCE_FUNDING_HISTORY_LIMIT,
        }
        start = requested.start_ms if requested else None
        end = requested.end_ms if requested else None
        if start is None:
            payload = await self.transport.get(f"{prefix}/fundingRate", base)
            return _relative_history(
                payload,
                "fundingTime",
                "fundingRate",
                requested,
                retrieved_at_ms=retrieved_at_ms,
            )
        cursor = start
        rows: dict[int, FundingObservation] = {}
        for _ in range(FUNDING_HISTORY_MAX_PAGES):
            params = {**base, "startTime": cursor}
            if end is not None:
                params["endTime"] = end
            payload = await self.transport.get(f"{prefix}/fundingRate", params)
            page = _relative_history(
                payload,
                "fundingTime",
                "fundingRate",
                requested,
                retrieved_at_ms=retrieved_at_ms,
            )
            for point in page:
                rows[point.timestamp_ms] = point
            if not page or len(page) < BINANCE_FUNDING_HISTORY_LIMIT:
                return tuple(rows[key] for key in sorted(rows))
            advanced = page[-1].timestamp_ms + 1
            if advanced <= cursor:
                raise PaginationError("funding history pagination did not advance")
            if end is not None and advanced > end:
                return tuple(rows[key] for key in sorted(rows))
            cursor = advanced
        raise PaginationError("funding history exceeded the bounded page limit")


class BybitFundingAdapter(NativeFundingAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "BYBIT"

    def capabilities(self, instrument: Instrument) -> FundingCapabilities:
        return FundingCapabilities(
            True,
            (FundingRateKind.INDICATIVE,),
            True,
            required_metadata=("contract_direction",),
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> FundingResult:
        category = _direction_category(instrument)
        ticker = await self.transport.get(
            "https://api.bybit.com/v5/market/tickers",
            {"category": category, "symbol": instrument.symbol},
        )
        _bybit_ok(ticker)
        rows = ticker.get("result", {}).get("list", [])
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise DataUnavailable("provider returned no indicative funding rate")
        row = rows[0]
        if row.get("fundingRate") is None or ticker.get("time") is None:
            raise DataUnavailable(
                "provider omitted the indicative funding rate or source time"
            )
        interval = _hour_interval(row.get("fundingIntervalHour"))
        retrieved_at_ms = int(self.clock() * 1_000)
        current = _relative_observation(
            int(ticker["time"]),
            row["fundingRate"],
            FundingRateKind.INDICATIVE,
            TemporalMode.CURRENT,
            interval,
            retrieved_at_ms=retrieved_at_ms,
            source_observation="funding.indicative_rate",
        )
        if not include_history:
            return FundingResult(current)
        try:
            history_rows = await self._history(
                instrument, category, history, retrieved_at_ms
            )
            return FundingResult(current, history_rows)
        except Exception as exc:
            return FundingResult(current, history_issue=self._issue(exc))

    async def _history(
        self,
        instrument: Instrument,
        category: str,
        requested: HistoryRange | None,
        retrieved_at_ms: int,
    ) -> tuple[FundingObservation, ...]:
        params: dict[str, Any] = {
            "category": category,
            "symbol": instrument.symbol,
            "limit": BYBIT_FUNDING_HISTORY_LIMIT,
        }
        if requested is not None and requested.start_ms is not None:
            params["startTime"] = requested.start_ms
            params["endTime"] = requested.end_ms or int(self.clock() * 1000)
        payload = await self.transport.get(
            "https://api.bybit.com/v5/market/funding/history", params
        )
        _bybit_ok(payload)
        rows = payload.get("result", {}).get("list", [])
        return _relative_history(
            rows,
            "fundingRateTimestamp",
            "fundingRate",
            requested,
            retrieved_at_ms=retrieved_at_ms,
        )


class GateFundingAdapter(NativeFundingAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "GATE"

    def capabilities(self, instrument: Instrument) -> FundingCapabilities:
        return FundingCapabilities(
            True,
            (FundingRateKind.INDICATIVE,),
            True,
            required_metadata=("settlement_currency",),
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> FundingResult:
        if not instrument.settlement_currency:
            raise InvalidInstrument(
                "settlement_currency is required for the provider endpoint identity"
            )
        settle = instrument.settlement_currency.lower()
        details = await self.transport.get(
            f"https://api.gateio.ws/api/v4/futures/{settle}/contracts/{instrument.symbol}"
        )
        if not isinstance(details, dict) or details.get("funding_rate") is None:
            raise DataUnavailable("provider omitted the indicative funding rate")
        interval = _seconds_interval(details.get("funding_interval"))
        retrieved_at_ms = int(self.clock() * 1000)
        current = _relative_observation(
            None,
            details["funding_rate"],
            FundingRateKind.INDICATIVE,
            TemporalMode.CURRENT,
            interval,
            retrieved_at_ms=retrieved_at_ms,
            source_observation="funding.indicative_rate",
        )
        if not include_history:
            return FundingResult(current)
        try:
            params: dict[str, Any] = {
                "contract": instrument.symbol,
                "limit": GATE_FUNDING_HISTORY_LIMIT,
            }
            if history is not None and history.start_ms is not None:
                params["from"] = history.start_ms // 1000
            if history is not None and history.end_ms is not None:
                params["to"] = history.end_ms // 1000
            payload = await self.transport.get(
                f"https://api.gateio.ws/api/v4/futures/{settle}/funding_rate",
                params,
            )
            rows = _relative_history(
                payload,
                "t",
                "r",
                history,
                timestamp_scale=1_000,
                retrieved_at_ms=retrieved_at_ms,
            )
            return FundingResult(current, rows)
        except Exception as exc:
            return FundingResult(current, history_issue=self._issue(exc))


class OkxFundingAdapter(NativeFundingAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "OKX"

    def capabilities(self, instrument: Instrument) -> FundingCapabilities:
        return FundingCapabilities(
            True,
            (FundingRateKind.INDICATIVE, FundingRateKind.SETTLED),
            True,
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> FundingResult:
        current_payload = await self.transport.get(
            f"{OKX_PUBLIC_API_URL}/funding-rate",
            {"instId": instrument.symbol},
        )
        current_row = _okx_row(current_payload, instrument.symbol, "current funding")
        if current_row.get("fundingRate") is None or current_row.get("ts") is None:
            raise DataUnavailable(
                "provider omitted the indicative funding rate or source time"
            )
        retrieved_at_ms = int(self.clock() * 1_000)
        current = _relative_observation(
            _integer_timestamp_ms(current_row["ts"]),
            current_row["fundingRate"],
            FundingRateKind.INDICATIVE,
            TemporalMode.CURRENT,
            _okx_interval(current_row),
            retrieved_at_ms=retrieved_at_ms,
            source_observation="funding.indicative_rate",
        )
        if not include_history:
            return FundingResult(current)
        try:
            payload = await self.transport.get(
                f"{OKX_PUBLIC_API_URL}/funding-rate-history",
                {"instId": instrument.symbol, "limit": 100},
            )
            _okx_payload(payload, "settled funding history")
            requested_rows = _relative_history(
                payload["data"],
                "fundingTime",
                "realizedRate",
                history,
                retrieved_at_ms=retrieved_at_ms,
            )
            return FundingResult(current, requested_rows)
        except Exception as exc:
            return FundingResult(current, history_issue=self._issue(exc))


class HyperliquidFundingAdapter(NativeFundingAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "HYPERLIQUID"

    def capabilities(self, instrument: Instrument) -> FundingCapabilities:
        return FundingCapabilities(
            True,
            (FundingRateKind.SETTLED,),
            True,
            declared_interval=explicit_interval(HYPERLIQUID_FUNDING_INTERVAL_SECONDS),
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> FundingResult:
        _, native_symbol = HyperliquidAdapter._scope_and_symbol(instrument)
        requested_end = (
            history.end_ms
            if history is not None and history.end_ms is not None
            else int(self.clock() * 1000)
        )
        requested_start = (
            history.start_ms
            if history is not None and history.start_ms is not None
            else requested_end
            - (
                7 * 86_400_000
                if include_history
                else 2 * HYPERLIQUID_FUNDING_INTERVAL_MS
            )
        )
        interval = explicit_interval(HYPERLIQUID_FUNDING_INTERVAL_SECONDS)
        retrieved_at_ms = int(self.clock() * 1_000)
        payload = await self.transport.post(
            "https://api.hyperliquid.xyz/info",
            {
                "type": "fundingHistory",
                "coin": native_symbol,
                "startTime": requested_start,
                "endTime": requested_end,
            },
        )
        points = _relative_history(
            payload,
            "time",
            "fundingRate",
            HistoryRange(requested_start, requested_end),
            interval=interval,
            retrieved_at_ms=retrieved_at_ms,
        )
        if not points:
            raise DataUnavailable("provider returned no settled funding rate")
        current_points = _relative_history(
            payload,
            "time",
            "fundingRate",
            None,
            interval=interval,
            temporal_mode=TemporalMode.SETTLED,
            retrieved_at_ms=retrieved_at_ms,
        )
        current = current_points[-1]
        now = int(self.clock() * 1000)
        if requested_end < now - HYPERLIQUID_FUNDING_INTERVAL_MS:
            current_payload = await self.transport.post(
                "https://api.hyperliquid.xyz/info",
                {
                    "type": "fundingHistory",
                    "coin": native_symbol,
                    "startTime": now - 2 * HYPERLIQUID_FUNDING_INTERVAL_MS,
                    "endTime": now,
                },
            )
            current_points = _relative_history(
                current_payload,
                "time",
                "fundingRate",
                None,
                interval=interval,
                temporal_mode=TemporalMode.SETTLED,
                retrieved_at_ms=int(self.clock() * 1_000),
            )
            if not current_points:
                raise DataUnavailable(
                    "provider returned no latest settled funding rate"
                )
            current = current_points[-1]
        return FundingResult(current, points if include_history else ())


class KrakenFundingAdapter(NativeFundingAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "KRAKEN"

    def capabilities(self, instrument: Instrument) -> FundingCapabilities:
        return FundingCapabilities(
            True,
            (FundingRateKind.INDICATIVE,),
            True,
            history_requires_start=True,
            declared_interval=explicit_interval(KRAKEN_FUNDING_INTERVAL_SECONDS),
            required_metadata=("contract_direction",),
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> FundingResult:
        payload = await self.transport.get(KRAKEN_TICKERS_URL)
        row, timestamp = _kraken_current(payload, instrument.symbol)
        mark = number(row.get("markPrice"))
        if mark <= 0:
            raise InvalidResponse("provider returned a non-positive mark price")
        absolute = number(row.get("fundingRate"))
        if instrument.contract_direction is ContractDirection.LINEAR:
            rate = absolute / mark
            step = "perp_md.funding.absolute_to_relative.linear.v1"
        elif instrument.contract_direction is ContractDirection.INVERSE:
            rate = absolute * mark
            step = "perp_md.funding.absolute_to_relative.inverse.v1"
        else:
            raise InvalidInstrument(
                "contract_direction is required to normalize an absolute funding amount"
            )
        interval = explicit_interval(KRAKEN_FUNDING_INTERVAL_SECONDS)
        retrieved_at_ms = int(self.clock() * 1_000)
        current = funding_observation(
            source_time_ms=timestamp,
            retrieved_at_ms=retrieved_at_ms,
            rate=rate,
            kind=FundingRateKind.INDICATIVE,
            temporal_mode=TemporalMode.CURRENT,
            interval=interval,
            source_observation="funding.absolute_amount",
            source_value=absolute,
            mark_price=mark,
            conversion_method=step,
        )
        if not include_history:
            return FundingResult(current)
        if history is None or history.start_ms is None:
            return FundingResult(
                current,
                history_issue=HistoryIssue(
                    "history_range_required",
                    "the full-retained funding endpoint requires an explicit history start",
                    retryable=False,
                ),
            )
        try:
            payload = await self.transport.get(
                KRAKEN_FUNDING_HISTORY_URL, {"symbol": instrument.symbol}
            )
            rows = _kraken_history(payload, history, interval, retrieved_at_ms)
            return FundingResult(current, rows)
        except Exception as exc:
            return FundingResult(current, history_issue=self._issue(exc))


def native_funding_adapters(
    transport: JsonTransport,
) -> dict[str, NativeFundingAdapter]:
    adapters: list[NativeFundingAdapter] = [
        BinanceFundingAdapter(transport),
        BybitFundingAdapter(transport),
        GateFundingAdapter(transport),
        OkxFundingAdapter(transport),
        HyperliquidFundingAdapter(transport),
        KrakenFundingAdapter(transport),
    ]
    return {
        venue: adapter
        for adapter in adapters
        for venue in {
            BinanceFundingAdapter: ("BINANCE",),
            BybitFundingAdapter: ("BYBIT",),
            GateFundingAdapter: ("GATE",),
            OkxFundingAdapter: ("OKX",),
            HyperliquidFundingAdapter: ("HYPERLIQUID",),
            KrakenFundingAdapter: ("KRAKEN",),
        }[type(adapter)]
    }


def _relative_observation(
    source_time_ms: int | None,
    rate: Any,
    kind: FundingRateKind,
    temporal_mode: TemporalMode,
    interval: FundingIntervalV1 | None = None,
    *,
    retrieved_at_ms: int,
    source_observation: str,
) -> FundingObservation:
    return funding_observation(
        source_time_ms=source_time_ms,
        retrieved_at_ms=retrieved_at_ms,
        rate=rate,
        kind=kind,
        temporal_mode=temporal_mode,
        interval=interval or unspecified_interval(),
        source_observation=source_observation,
        source_value=rate,
    )


def _relative_history(
    payload: Any,
    timestamp_field: str,
    rate_field: str,
    requested: HistoryRange | None,
    *,
    timestamp_scale: int = 1,
    interval: FundingIntervalV1 | None = None,
    temporal_mode: TemporalMode = TemporalMode.HISTORICAL,
    retrieved_at_ms: int,
) -> tuple[FundingObservation, ...]:
    if not isinstance(payload, list):
        raise InvalidResponse("provider returned an invalid settled funding history")
    rows: dict[int, FundingObservation] = {}
    for source in payload:
        if not isinstance(source, dict):
            raise InvalidResponse(
                "provider returned an invalid settled funding history row"
            )
        try:
            timestamp = int(float(source[timestamp_field]) * timestamp_scale)
            point = _relative_observation(
                timestamp,
                source[rate_field],
                FundingRateKind.SETTLED,
                temporal_mode,
                interval,
                retrieved_at_ms=retrieved_at_ms,
                source_observation="funding.settled_rate",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidResponse(
                "provider returned an invalid settled funding history row"
            ) from exc
        if requested is not None:
            if requested.start_ms is not None and timestamp < requested.start_ms:
                continue
            if requested.end_ms is not None and timestamp > requested.end_ms:
                continue
        existing = rows.get(timestamp)
        if existing is not None and existing.rate != point.rate:
            raise InvalidResponse("provider returned conflicting settled funding rows")
        rows[timestamp] = point
    return preserve_observed_intervals(tuple(rows[key] for key in sorted(rows)))


def _direction_category(instrument: Instrument) -> str:
    if instrument.contract_direction is ContractDirection.LINEAR:
        return "linear"
    if instrument.contract_direction is ContractDirection.INVERSE:
        return "inverse"
    raise InvalidInstrument(
        "contract_direction is required for provider product routing"
    )


def _inverse_product(instrument: Instrument) -> bool:
    if instrument.contract_direction is ContractDirection.INVERSE:
        return True
    if instrument.contract_direction is ContractDirection.LINEAR:
        return False
    product = str(instrument.product or "").upper()
    if product == "COIN-M":
        return True
    if product == "USD-M":
        return False
    raise InvalidInstrument(
        "contract_direction is required for provider product routing"
    )


def _bybit_ok(payload: Any) -> None:
    if not isinstance(payload, dict) or str(payload.get("retCode")) != "0":
        raise InvalidResponse("provider rejected the funding request")


def _okx_payload(payload: Any, description: str) -> None:
    if (
        not isinstance(payload, dict)
        or str(payload.get("code")) != "0"
        or not isinstance(payload.get("data"), list)
    ):
        raise InvalidResponse(f"provider returned an invalid {description}")


def _okx_row(payload: Any, symbol: str, description: str) -> dict[str, Any]:
    _okx_payload(payload, description)
    matches = [
        row
        for row in payload["data"]
        if isinstance(row, dict) and row.get("instId") == symbol
    ]
    if len(matches) != 1:
        raise DataUnavailable(
            f"instrument does not resolve to exactly one {description} row"
        )
    return matches[0]


def _okx_interval(row: dict[str, Any]) -> FundingIntervalV1:
    start = row.get("fundingTime")
    end = row.get("nextFundingTime")
    if start in (None, "") or end in (None, ""):
        return unspecified_interval()
    start_ms = _integer_timestamp_ms(start)
    end_ms = _integer_timestamp_ms(end)
    return explicit_interval(funding_window_duration_seconds(start_ms, end_ms))


def _integer_timestamp_ms(value: Any) -> int:
    if isinstance(value, bool):
        raise InvalidResponse("provider returned an invalid source timestamp")
    try:
        parsed = number(value)
    except (TypeError, ValueError) as exc:
        raise InvalidResponse("provider returned an invalid source timestamp") from exc
    if not parsed.is_integer() or parsed < 0:
        raise InvalidResponse("provider returned an invalid source timestamp")
    return int(parsed)


def _seconds_interval(value: Any) -> FundingIntervalV1:
    if value in (None, "", 0, "0"):
        return unspecified_interval()
    seconds = number(value)
    if not seconds.is_integer() or seconds <= 0:
        raise InvalidResponse("provider returned an invalid funding interval")
    return explicit_interval(int(seconds))


def _hour_interval(value: Any) -> FundingIntervalV1:
    if value in (None, "", 0, "0"):
        return unspecified_interval()
    hours = number(value)
    duration = hours * 3_600
    if not duration.is_integer() or duration <= 0:
        raise InvalidResponse("provider returned an invalid funding interval")
    return explicit_interval(int(duration))


def _kraken_current(payload: Any, symbol: str) -> tuple[dict[str, Any], int]:
    if not isinstance(payload, dict) or payload.get("result") != "success":
        raise InvalidResponse("provider rejected the aggregate funding request")
    rows = payload.get("tickers")
    if not isinstance(rows, list):
        raise InvalidResponse("provider returned an invalid aggregate funding response")
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("symbol"), str)
        or not row["symbol"]
        for row in rows
    ):
        raise InvalidResponse("provider returned an invalid aggregate funding row")
    matches = [row for row in rows if row["symbol"] == symbol]
    if len(matches) != 1:
        raise DataUnavailable(
            "instrument does not resolve to exactly one aggregate funding row"
        )
    return matches[0], _timestamp_ms(payload.get("serverTime"))


def _kraken_history(
    payload: Any,
    requested: HistoryRange,
    interval: FundingIntervalV1,
    retrieved_at_ms: int,
) -> tuple[FundingObservation, ...]:
    if not isinstance(payload, dict) or payload.get("result") != "success":
        raise InvalidResponse("provider rejected the settled funding history request")
    rows = payload.get("rates")
    if not isinstance(rows, list):
        raise InvalidResponse("provider returned an invalid settled funding history")
    if len(rows) > KRAKEN_FUNDING_HISTORY_MAX_ROWS:
        raise PaginationError("funding history exceeded the bounded row limit")
    normalized: dict[int, FundingObservation] = {}
    for source in rows:
        if not isinstance(source, dict):
            raise InvalidResponse(
                "provider returned an invalid settled funding history row"
            )
        try:
            timestamp = _timestamp_ms(source["timestamp"])
            point = _relative_observation(
                timestamp,
                source["relativeFundingRate"],
                FundingRateKind.SETTLED,
                TemporalMode.HISTORICAL,
                interval,
                retrieved_at_ms=retrieved_at_ms,
                source_observation="funding.settled_rate",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidResponse(
                "provider returned an invalid settled funding history row"
            ) from exc
        if requested.start_ms is not None and timestamp < requested.start_ms:
            continue
        if requested.end_ms is not None and timestamp > requested.end_ms:
            continue
        existing = normalized.get(timestamp)
        if existing is not None and existing.rate != point.rate:
            raise InvalidResponse("provider returned conflicting settled funding rows")
        normalized[timestamp] = point
    return tuple(normalized[key] for key in sorted(normalized))


def _timestamp_ms(value: Any) -> int:
    if isinstance(value, bool):
        raise InvalidResponse("provider returned an invalid source timestamp")
    if isinstance(value, (int, float)):
        parsed = number(value)
        timestamp = int(parsed * 1_000) if parsed < 10_000_000_000 else int(parsed)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed_time = datetime.fromisoformat(
                text.removesuffix("Z") + ("+00:00" if text.endswith("Z") else "")
            )
        except ValueError as exc:
            raise InvalidResponse(
                "provider returned an invalid source timestamp"
            ) from exc
        if parsed_time.tzinfo is None:
            raise InvalidResponse("provider returned a timezone-free source timestamp")
        timestamp = int(parsed_time.timestamp() * 1_000)
    else:
        raise InvalidResponse("provider returned an invalid source timestamp")
    if timestamp < 0:
        raise InvalidResponse("provider returned a negative source timestamp")
    return timestamp
