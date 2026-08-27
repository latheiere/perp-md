from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from cdm import OpenInterestMeasure, OpenInterestValueV1

from perp_md.errors import (
    DataUnavailable,
    InvalidInstrument,
    InvalidResponse,
    PaginationError,
)
from perp_md.identity import (
    REST_DERIVATIVE_STATUS_INSTRUMENT,
    REST_PAIR,
    REST_PRODUCT_FAMILY,
    RPC_INSTRUMENT,
    RPC_PRODUCT_FAMILY,
    ReferenceInstrument,
    adapter_identity,
    optional_adapter_identity,
)
from perp_md.models import (
    ContractDirection,
    HistoryIssue,
    HistoryRange,
    Instrument,
    NativeUnit,
    ObservationTimeKind,
    OpenInterestCapabilities,
    OpenInterestObservation,
    OpenInterestResult,
    ValuationMethod,
)
from perp_md.normalization import contract_value_usd, number, proven_base_quantity
from perp_md.transport import JsonTransport

BINANCE_HISTORY_LIMIT = 500
BYBIT_HISTORY_LIMIT = 200
BYBIT_MARK_HISTORY_LIMIT = 1_000
GATE_HISTORY_LIMIT = 1_000
HISTORY_MAX_PAGES = 200
BINANCE_HISTORY_DAYS = 30
HISTORY_BUCKET_MS = 300_000
BITFINEX_HISTORY_LIMIT = 5_000
DEEPCOIN_HISTORY_LIMIT = 300
KUCOIN_HISTORY_LIMIT = 200
HTX_HISTORY_LIMIT = 200
HYPERLIQUID_SCOPED_PRODUCT_FAMILY = "HIP-3"
KRAKEN_HISTORY_DAYS = 6
KRAKEN_HISTORY_INTERVAL_SECONDS = 300
KRAKEN_TICKERS_URL = "https://futures.kraken.com/derivatives/api/v3/tickers"
KRAKEN_CHARTS_URL = "https://futures.kraken.com/api/charts/v1"


@dataclass
class NativeAdapter:
    transport: JsonTransport
    clock: Callable[[], float] = time.time

    async def close(self) -> None:
        return None

    @staticmethod
    def _issue(exc: Exception) -> HistoryIssue:
        detail = str(exc).strip()
        message = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        return HistoryIssue("history_unavailable", message)


class BinanceAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "BINANCE"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        required = ("contract_multiplier",) if self._inverse(instrument) else ()
        return OpenInterestCapabilities(
            True, True, 300, BINANCE_HISTORY_DAYS, required
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        inverse = self._inverse(instrument)
        prefix = (
            "https://dapi.binance.com/dapi/v1"
            if inverse
            else "https://fapi.binance.com/fapi/v1"
        )
        history_url = (
            "https://dapi.binance.com/futures/data/openInterestHist"
            if inverse
            else "https://fapi.binance.com/futures/data/openInterestHist"
        )
        params: dict[str, Any] = {"period": "5m", "limit": BINANCE_HISTORY_LIMIT}
        if not inverse:
            params["symbol"] = instrument.symbol
        oi, premium = await asyncio.gather(
            self.transport.get(f"{prefix}/openInterest", {"symbol": instrument.symbol}),
            self.transport.get(f"{prefix}/premiumIndex", {"symbol": instrument.symbol}),
        )
        raw = number(oi["openInterest"])
        mark = (
            number(premium["markPrice"])
            if premium.get("markPrice") is not None
            else None
        )
        if inverse:
            value = contract_value_usd(instrument, raw, mark)
            valuation = ValuationMethod.CONTRACT_VALUE
        else:
            if mark is None:
                raise InvalidResponse("venue omitted mark price")
            value = raw * mark
            valuation = ValuationMethod.MARK_PRICE
        native_unit = NativeUnit.CONTRACTS if inverse else NativeUnit.BASE
        current = OpenInterestObservation(
            int(oi.get("time") or self.clock() * 1000),
            value,
            raw,
            native_unit,
            mark,
            valuation,
            proven_base_quantity(instrument, raw, native_unit),
            ObservationTimeKind.SOURCE
            if oi.get("time") is not None
            else ObservationTimeKind.RETRIEVED,
        )
        if not include_history:
            return OpenInterestResult(current)
        try:
            if inverse:
                if isinstance(instrument, Instrument) and instrument.pair_symbol is None:
                    raise InvalidInstrument(
                        "pair_symbol is required for the provider history identity"
                    )
                pair = adapter_identity(
                    instrument,
                    REST_PAIR,
                    legacy_value=instrument.pair_symbol,
                )
                contract_type = (
                    adapter_identity(
                        instrument,
                        REST_PRODUCT_FAMILY,
                        legacy_value=None,
                    )
                    if instrument.market_type == "future"
                    else "PERPETUAL"
                )
                params.update({"pair": pair, "contractType": contract_type})
            payload = await self._history(history_url, params, history)
            rows: list[OpenInterestObservation] = []
            for row in payload:
                native = number(row["sumOpenInterest"])
                unit = NativeUnit.CONTRACTS if inverse else NativeUnit.BASE
                rows.append(
                    OpenInterestObservation(
                        int(row["timestamp"]),
                        native * number(instrument.contract_multiplier)
                        if inverse
                        else number(row["sumOpenInterestValue"]),
                        native,
                        unit,
                        valuation=ValuationMethod.CONTRACT_VALUE
                        if inverse
                        else ValuationMethod.VENUE_REPORTED,
                        base_quantity=proven_base_quantity(instrument, native, unit),
                    )
                )
            return OpenInterestResult(current, tuple(rows))
        except Exception as exc:
            return OpenInterestResult(current, history_issue=self._issue(exc))

    async def _history(
        self,
        url: str,
        base_params: dict[str, Any],
        requested: HistoryRange | None,
    ) -> list[dict[str, Any]]:
        if requested is None or requested.start_ms is None:
            payload = await self.transport.get(url, base_params)
            if not isinstance(payload, list):
                raise InvalidResponse("venue returned an invalid open-interest history")
            return payload
        current_bucket = (
            int(self.clock() * 1000) // HISTORY_BUCKET_MS * HISTORY_BUCKET_MS
        )
        available_start = (
            current_bucket - BINANCE_HISTORY_DAYS * 86_400_000 + HISTORY_BUCKET_MS
        )
        start = max(requested.start_ms, available_start)
        end = min(requested.end_ms or current_bucket, current_bucket)
        if start > end:
            return []
        page_end = end
        rows: dict[int, dict[str, Any]] = {}
        for _ in range(HISTORY_MAX_PAGES):
            payload = await self.transport.get(
                url, {**base_params, "startTime": start, "endTime": page_end}
            )
            if not isinstance(payload, list):
                raise InvalidResponse("venue returned an invalid open-interest history")
            if not payload:
                return [rows[key] for key in sorted(rows)]
            page = sorted(payload, key=lambda row: int(row["timestamp"]))
            for row in page:
                timestamp = int(row["timestamp"])
                if start <= timestamp <= end:
                    rows[timestamp] = row
            oldest = int(page[0]["timestamp"])
            if len(page) < int(base_params["limit"]) or oldest <= start:
                return [rows[key] for key in sorted(rows)]
            next_end = oldest - 1
            if next_end >= page_end:
                raise PaginationError(
                    "open-interest history pagination did not advance"
                )
            page_end = next_end
        raise PaginationError("open-interest history exceeded the bounded page limit")

    @staticmethod
    def _inverse(instrument: Instrument) -> bool:
        return (
            instrument.contract_direction is ContractDirection.INVERSE
            or str(instrument.product or "").upper() == "COIN-M"
        )


class BybitAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "BYBIT"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, True, 300)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        category = (
            "inverse"
            if instrument.contract_direction is ContractDirection.INVERSE
            else "linear"
        )
        ticker = await self.transport.get(
            "https://api.bybit.com/v5/market/tickers",
            {"category": category, "symbol": instrument.symbol},
        )
        self._ok(ticker)
        tickers = ticker.get("result", {}).get("list", [])
        if not tickers:
            raise DataUnavailable("venue returned no current open interest")
        row = tickers[0]
        mark = number(row.get("markPrice") or row.get("lastPrice"))
        raw = (
            number(row["openInterest"]) if row.get("openInterest") is not None else None
        )
        native_unit = NativeUnit.QUOTE if category == "inverse" else NativeUnit.BASE
        base_quantity = (
            OpenInterestValueV1(
                Decimal(str(raw)), OpenInterestMeasure.BASE_QUANTITY
            )
            if raw is not None and category == "linear"
            else None
        )
        if row.get("openInterestValue") is not None:
            value = number(row["openInterestValue"])
            valuation = ValuationMethod.VENUE_REPORTED
        elif raw is not None:
            value = raw if category == "inverse" else raw * mark
            valuation = (
                ValuationMethod.VENUE_REPORTED
                if category == "inverse"
                else ValuationMethod.MARK_PRICE
            )
        else:
            raise DataUnavailable("venue omitted current open interest")
        current = OpenInterestObservation(
            int(ticker.get("time") or self.clock() * 1000),
            value,
            raw,
            native_unit,
            mark,
            valuation,
            base_quantity,
            ObservationTimeKind.SOURCE
            if ticker.get("time") is not None
            else ObservationTimeKind.RETRIEVED,
        )
        if not include_history:
            return OpenInterestResult(current)
        try:
            payload = await self._history(instrument, category, history)
            if category == "linear":
                marks = (
                    await self._mark_history(
                        instrument,
                        min(int(item["timestamp"]) for item in payload),
                        max(int(item["timestamp"]) for item in payload),
                    )
                    if payload
                    else {}
                )
            else:
                marks = {}
            observations: list[OpenInterestObservation] = []
            missing_marks = 0
            for item in payload:
                timestamp = int(item["timestamp"])
                native = number(item["openInterest"])
                if category == "inverse":
                    observations.append(
                        OpenInterestObservation(
                            timestamp,
                            native,
                            native,
                            NativeUnit.QUOTE,
                            valuation=ValuationMethod.VENUE_REPORTED,
                        )
                    )
                    continue
                historical_mark = marks.get(timestamp)
                if historical_mark is None:
                    missing_marks += 1
                    continue
                base = OpenInterestValueV1(
                    Decimal(str(native)), OpenInterestMeasure.BASE_QUANTITY
                )
                observations.append(
                    OpenInterestObservation(
                        timestamp,
                        native * historical_mark,
                        native,
                        NativeUnit.BASE,
                        historical_mark,
                        ValuationMethod.MARK_PRICE,
                        base,
                    )
                )
            issue = None
            if missing_marks:
                issue = HistoryIssue(
                    "history_partial",
                    "mark-price history omitted "
                    f"{missing_marks} of {len(payload)} open-interest buckets",
                )
            return OpenInterestResult(current, tuple(observations), issue)
        except Exception as exc:
            return OpenInterestResult(current, history_issue=self._issue(exc))

    async def _history(
        self,
        instrument: Instrument,
        category: str,
        requested: HistoryRange | None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "category": category,
            "symbol": instrument.symbol,
            "intervalTime": "5min",
            "limit": BYBIT_HISTORY_LIMIT,
        }
        if requested is not None and requested.start_ms is not None:
            current_bucket = (
                int(self.clock() * 1000) // HISTORY_BUCKET_MS * HISTORY_BUCKET_MS
            )
            params["startTime"] = requested.start_ms
            params["endTime"] = min(requested.end_ms or current_bucket, current_bucket)
            if params["startTime"] > params["endTime"]:
                return []
        rows: dict[int, dict[str, Any]] = {}
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(HISTORY_MAX_PAGES):
            request = dict(params)
            if cursor is not None:
                request["cursor"] = cursor
            payload = await self.transport.get(
                "https://api.bybit.com/v5/market/open-interest", request
            )
            self._ok(payload)
            result = payload.get("result", {})
            page = result.get("list", []) if isinstance(result, dict) else None
            if not isinstance(page, list):
                raise InvalidResponse("venue returned an invalid open-interest history")
            for item in page:
                timestamp = int(item["timestamp"])
                if (
                    "startTime" not in params
                    or params["startTime"] <= timestamp <= params["endTime"]
                ):
                    rows[timestamp] = item
            next_cursor = result.get("nextPageCursor")
            if not next_cursor:
                return [rows[key] for key in sorted(rows)]
            if not isinstance(next_cursor, str) or next_cursor in seen:
                raise PaginationError(
                    "open-interest history returned an invalid cursor"
                )
            seen.add(next_cursor)
            cursor = next_cursor
        raise PaginationError("open-interest history exceeded the bounded page limit")

    async def _mark_history(
        self,
        instrument: Instrument,
        start_ms: int,
        end_ms: int,
    ) -> dict[int, float]:
        page_end = end_ms
        marks: dict[int, float] = {}
        for _ in range(HISTORY_MAX_PAGES):
            payload = await self.transport.get(
                "https://api.bybit.com/v5/market/mark-price-kline",
                {
                    "category": "linear",
                    "symbol": instrument.symbol,
                    "interval": "5",
                    "limit": BYBIT_MARK_HISTORY_LIMIT,
                    "start": start_ms,
                    "end": page_end,
                },
            )
            self._ok(payload)
            result = payload.get("result", {})
            page = result.get("list", []) if isinstance(result, dict) else None
            if not isinstance(page, list):
                raise InvalidResponse("venue returned an invalid mark-price history")
            if not page:
                return marks
            ordered = sorted(page, key=lambda item: int(item[0]))
            for item in ordered:
                if not isinstance(item, list) or len(item) < 2:
                    raise InvalidResponse("venue returned an invalid mark-price candle")
                timestamp = int(item[0])
                if start_ms <= timestamp <= end_ms:
                    mark = number(item[1])
                    if mark <= 0:
                        raise InvalidResponse(
                            "venue returned a non-positive historical mark price"
                        )
                    marks[timestamp] = mark
            oldest = int(ordered[0][0])
            if len(ordered) < BYBIT_MARK_HISTORY_LIMIT or oldest <= start_ms:
                return marks
            advanced = oldest - 1
            if advanced >= page_end:
                raise PaginationError("mark-price history pagination did not advance")
            page_end = advanced
        raise PaginationError("mark-price history exceeded the bounded page limit")

    @staticmethod
    def _ok(payload: Any) -> None:
        if not isinstance(payload, dict) or str(payload.get("retCode")) != "0":
            raise InvalidResponse("venue rejected the request")


class GateAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "GATE"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(
            True,
            instrument.market_type != "future",
            300 if instrument.market_type != "future" else None,
            required_metadata=("settlement_currency",),
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        if not instrument.settlement_currency:
            raise InvalidInstrument(
                "settlement_currency is required for the provider endpoint identity"
            )
        settle = instrument.settlement_currency.lower()
        route = "delivery" if instrument.market_type == "future" else "futures"
        details = await self.transport.get(
            f"https://api.gateio.ws/api/v4/{route}/{settle}/contracts/{instrument.symbol}"
        )
        position = number(details["position_size"])
        mark = number(details["mark_price"])
        native = position * 2
        if str(details.get("type", "")).lower() == "inverse":
            value = native
            valuation = ValuationMethod.CONTRACT_VALUE
        else:
            value = native * number(details["quanto_multiplier"]) * mark
            valuation = ValuationMethod.MARK_PRICE
        base_quantity = None
        if str(details.get("type", "")).lower() != "inverse":
            source_multiplier = number(details.get("quanto_multiplier"))
            base_quantity = OpenInterestValueV1(
                Decimal(str(native * source_multiplier)),
                OpenInterestMeasure.BASE_QUANTITY,
            )
        current = OpenInterestObservation(
            int(self.clock() * 1000),
            value,
            native,
            NativeUnit.CONTRACTS,
            mark,
            valuation,
            base_quantity,
            ObservationTimeKind.RETRIEVED,
        )
        if not include_history:
            return OpenInterestResult(current)
        if instrument.market_type == "future":
            return OpenInterestResult(current)
        try:
            payload = await self._history(settle, instrument.symbol, history)
            observations: list[OpenInterestObservation] = []
            for item in payload:
                native = number(item["open_interest"])
                notional = number(item["open_interest_usd"])
                mark = number(item["mark_price"])
                if mark <= 0:
                    raise InvalidResponse(
                        "venue returned a non-positive historical mark price"
                    )
                base = OpenInterestValueV1(
                    Decimal(str(notional)) / Decimal(str(mark)),
                    OpenInterestMeasure.BASE_QUANTITY,
                )
                observations.append(
                    OpenInterestObservation(
                        int(item["time"]) * 1000,
                        notional,
                        native,
                        NativeUnit.CONTRACTS,
                        mark,
                        ValuationMethod.VENUE_REPORTED,
                        base,
                    )
                )
            return OpenInterestResult(current, tuple(observations))
        except Exception as exc:
            return OpenInterestResult(current, history_issue=self._issue(exc))

    async def _history(
        self,
        settle: str,
        symbol: str,
        requested: HistoryRange | None,
    ) -> list[dict[str, Any]]:
        url = f"https://api.gateio.ws/api/v4/futures/{settle}/contract_stats"
        base = {"contract": symbol, "interval": "5m", "limit": GATE_HISTORY_LIMIT}
        if requested is None or requested.start_ms is None:
            payload = await self.transport.get(url, base)
            if not isinstance(payload, list):
                raise InvalidResponse("venue returned an invalid open-interest history")
            return payload
        current_bucket = (
            int(self.clock() * 1000) // HISTORY_BUCKET_MS * HISTORY_BUCKET_MS
        )
        end = min(requested.end_ms or current_bucket, current_bucket)
        next_from = (requested.start_ms + 999) // 1000
        if next_from * 1000 > end:
            return []
        rows: dict[int, dict[str, Any]] = {}
        for _ in range(HISTORY_MAX_PAGES):
            payload = await self.transport.get(url, {**base, "from": next_from})
            if not isinstance(payload, list):
                raise InvalidResponse("venue returned an invalid open-interest history")
            if not payload:
                return [rows[key] for key in sorted(rows)]
            page = sorted(payload, key=lambda row: int(row["time"]))
            for item in page:
                timestamp = int(item["time"])
                if next_from <= timestamp <= end // 1000:
                    rows[timestamp] = item
            newest = int(page[-1]["time"])
            # Statistics pages can omit native buckets and therefore contain
            # fewer rows than the requested limit before the requested range
            # has been traversed. Only the source timestamp proves completion.
            if newest * 1000 >= end:
                return [rows[key] for key in sorted(rows)]
            advanced = newest + 1
            if advanced <= next_from:
                raise PaginationError(
                    "open-interest history pagination did not advance"
                )
            next_from = advanced
        raise PaginationError("open-interest history exceeded the bounded page limit")


class BitfinexAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "BITFINEX"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(
            True,
            True,
            required_metadata=("contract_direction", "contract_multiplier"),
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        legacy_symbol = (
            instrument.pair_symbol or f"t{instrument.symbol}"
            if isinstance(instrument, Instrument)
            else None
        )
        endpoint_symbol = adapter_identity(
            instrument,
            REST_DERIVATIVE_STATUS_INSTRUMENT,
            legacy_value=legacy_symbol,
        )
        payload = await self.transport.get(
            "https://api-pub.bitfinex.com/v2/status/deriv",
            {"keys": endpoint_symbol},
        )
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], list)
        ):
            raise InvalidResponse("venue returned invalid derivative status")
        row = payload[0]
        if len(row) <= 18 or row[18] is None or row[15] is None or row[1] is None:
            raise DataUnavailable(
                "venue omitted open interest, mark price, or timestamp"
            )
        contracts, mark = number(row[18]), number(row[15])
        current = OpenInterestObservation(
            int(row[1]),
            contract_value_usd(instrument, contracts, mark),
            contracts,
            NativeUnit.CONTRACTS,
            mark,
            ValuationMethod.MARK_PRICE
            if instrument.contract_direction is ContractDirection.LINEAR
            else ValuationMethod.CONTRACT_VALUE,
            proven_base_quantity(instrument, contracts, NativeUnit.CONTRACTS),
        )
        if not include_history:
            return OpenInterestResult(current)
        try:
            rows = await self._history(endpoint_symbol, history)
            points = tuple(self._history_observation(instrument, row) for row in rows)
            return OpenInterestResult(current, points)
        except Exception as exc:
            return OpenInterestResult(current, history_issue=self._issue(exc))

    async def _history(
        self, symbol: str, requested: HistoryRange | None
    ) -> list[list[Any]]:
        url = f"https://api-pub.bitfinex.com/v2/status/deriv/{quote(symbol, safe='')}/hist"
        params: dict[str, Any] = {"sort": -1, "limit": BITFINEX_HISTORY_LIMIT}
        if requested is not None and requested.start_ms is not None:
            params["start"] = requested.start_ms
            params["end"] = requested.end_ms or int(self.clock() * 1_000)
        rows: dict[int, list[Any]] = {}
        page_end = params.get("end")
        for _ in range(HISTORY_MAX_PAGES):
            request = dict(params)
            if page_end is not None:
                request["end"] = page_end
            payload = await self.transport.get(url, request)
            if not isinstance(payload, list):
                raise InvalidResponse("venue returned an invalid derivative-status history")
            if not payload:
                return [rows[key] for key in sorted(rows)]
            for row in payload:
                if not isinstance(row, list) or len(row) <= 17:
                    raise InvalidResponse("venue changed the derivative-status history shape")
                timestamp = _integer_ms(row[0], "open-interest")
                if requested is None or (
                    (requested.start_ms is None or timestamp >= requested.start_ms)
                    and (requested.end_ms is None or timestamp <= requested.end_ms)
                ):
                    rows[timestamp] = row
            oldest = min(_integer_ms(row[0], "open-interest") for row in payload)
            start = requested.start_ms if requested else None
            if len(payload) < BITFINEX_HISTORY_LIMIT or (start is not None and oldest <= start):
                return [rows[key] for key in sorted(rows)]
            advanced = oldest - 1
            if page_end is not None and advanced >= page_end:
                raise PaginationError("open-interest history pagination did not advance")
            page_end = advanced
        raise PaginationError("open-interest history exceeded the bounded page limit")

    @staticmethod
    def _history_observation(
        instrument: Instrument, row: list[Any]
    ) -> OpenInterestObservation:
        if row[17] is None or row[14] is None:
            raise DataUnavailable("venue omitted historical open interest or mark price")
        contracts, mark = number(row[17]), number(row[14])
        return OpenInterestObservation(
            _integer_ms(row[0], "open-interest"),
            contract_value_usd(instrument, contracts, mark),
            contracts,
            NativeUnit.CONTRACTS,
            mark,
            ValuationMethod.MARK_PRICE
            if instrument.contract_direction is ContractDirection.LINEAR
            else ValuationMethod.CONTRACT_VALUE,
            proven_base_quantity(instrument, contracts, NativeUnit.CONTRACTS),
        )


class DeepcoinAdapter(NativeAdapter):
    BASE_URL = "https://api.deepcoin.com/deepcoin/v2/market"

    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "DEEPCOIN"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(
            True,
            True,
            300,
            required_metadata=("contract_direction", "contract_multiplier"),
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        params = {"instId": instrument.symbol, "bar": "5m", "limit": 1}
        oi_payload, mark_payload = await asyncio.gather(
            self.transport.get(f"{self.BASE_URL}/open-interest-volume", params),
            self.transport.get(
                f"{self.BASE_URL}/mark-price",
                {"instType": "SWAP", "instId": instrument.symbol},
            ),
        )
        oi_rows = _coded_rows(oi_payload, "open-interest snapshot")
        mark_rows = _coded_rows(mark_payload, "mark-price snapshot")
        if len(oi_rows) != 1 or len(mark_rows) != 1:
            raise DataUnavailable("instrument does not resolve to one current market row")
        oi_row, mark_row = oi_rows[0], mark_rows[0]
        timestamp = _integer_ms(oi_row.get("ts"), "open-interest")
        current = _contract_observation(
            instrument, timestamp, oi_row.get("oi"), mark_row.get("markPx")
        )
        if not include_history:
            return OpenInterestResult(current)
        try:
            oi_history = await self._history(instrument, history)
            marks = await self._marks(instrument, oi_history)
            points, missing = _join_contract_history(instrument, oi_history, marks)
            issue = _partial_mark_issue(missing, len(oi_history))
            return OpenInterestResult(current, points, issue)
        except Exception as exc:
            return OpenInterestResult(current, history_issue=self._issue(exc))

    async def _history(
        self, instrument: Instrument, requested: HistoryRange | None
    ) -> list[dict[str, Any]]:
        base: dict[str, Any] = {
            "instId": instrument.symbol,
            "bar": "5m",
            "limit": DEEPCOIN_HISTORY_LIMIT,
        }
        if requested and requested.start_ms is not None:
            base["startTime"] = requested.start_ms
            base["endTime"] = requested.end_ms or int(self.clock() * 1_000)
        return await _backward_dict_history(
            self.transport,
            f"{self.BASE_URL}/open-interest-volume",
            base,
            "ts",
            DEEPCOIN_HISTORY_LIMIT,
            requested,
            coded=True,
        )

    async def _marks(
        self, instrument: Instrument, rows: list[dict[str, Any]]
    ) -> dict[int, float]:
        if not rows:
            return {}
        start = min(_integer_ms(row.get("ts"), "open-interest") for row in rows)
        end = max(_integer_ms(row.get("ts"), "open-interest") for row in rows)
        marks: dict[int, float] = {}
        page_end = end
        for _ in range(HISTORY_MAX_PAGES):
            payload = await self.transport.get(
                f"{self.BASE_URL}/mark-price-candles",
                {
                    "instId": instrument.symbol,
                    "bar": "5m",
                    "startTime": start,
                    "endTime": page_end,
                    "limit": DEEPCOIN_HISTORY_LIMIT,
                },
            )
            candles = _coded_rows(payload, "mark-price history", row_type=list)
            if not candles:
                return marks
            timestamps: list[int] = []
            for candle in candles:
                if len(candle) < 5:
                    raise InvalidResponse("venue changed the mark-price candle shape")
                timestamp = _integer_ms(candle[0], "mark-price")
                timestamps.append(timestamp)
                marks[timestamp] = number(candle[4])
            oldest = min(timestamps)
            if len(candles) < DEEPCOIN_HISTORY_LIMIT or oldest <= start:
                return marks
            advanced = oldest - 1
            if advanced >= page_end:
                raise PaginationError("mark-price history pagination did not advance")
            page_end = advanced
        raise PaginationError("mark-price history exceeded the bounded page limit")


class KucoinAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "KUCOIN"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(
            True,
            True,
            300,
            7,
            ("contract_direction", "contract_multiplier"),
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        payload, current_payload = await asyncio.gather(
            self.transport.get(
                f"https://api-futures.kucoin.com/api/v1/contracts/{quote(instrument.symbol, safe='')}"
            ),
            self.transport.get(
                "https://api.kucoin.com/api/ua/v1/market/open-interest",
                {"symbol": instrument.symbol},
            ),
        )
        row = _kucoin_data(payload, "contract snapshot", dict)
        current_rows = _kucoin_data(current_payload, "open-interest snapshot", list)
        if len(current_rows) != 1 or not isinstance(current_rows[0], dict):
            raise DataUnavailable("instrument does not resolve to one current open-interest row")
        current_row = current_rows[0]
        current = _contract_observation(
            instrument,
            _integer_ms(current_row.get("ts"), "open-interest"),
            current_row.get("openInterest"),
            row.get("markPrice"),
        )
        if not include_history:
            return OpenInterestResult(current)
        try:
            rows = await self._history(instrument, history)
            marks = await self._marks(instrument, rows)
            points, missing = _join_contract_history(instrument, rows, marks)
            return OpenInterestResult(
                current, points, _partial_mark_issue(missing, len(rows))
            )
        except Exception as exc:
            return OpenInterestResult(current, history_issue=self._issue(exc))

    async def _history(
        self, instrument: Instrument, requested: HistoryRange | None
    ) -> list[dict[str, Any]]:
        base: dict[str, Any] = {
            "symbol": instrument.symbol,
            "interval": "5min",
            "pageSize": KUCOIN_HISTORY_LIMIT,
        }
        if requested and requested.start_ms is not None:
            base["startAt"] = requested.start_ms
            base["endAt"] = requested.end_ms or int(self.clock() * 1_000)
        return await _backward_dict_history(
            self.transport,
            "https://api.kucoin.com/api/ua/v1/market/open-interest",
            base,
            "ts",
            KUCOIN_HISTORY_LIMIT,
            requested,
            kucoin=True,
        )

    async def _marks(
        self, instrument: Instrument, rows: list[dict[str, Any]]
    ) -> dict[int, float]:
        if not rows:
            return {}
        start = min(_integer_ms(row.get("ts"), "open-interest") for row in rows)
        end = max(_integer_ms(row.get("ts"), "open-interest") for row in rows)
        marks: dict[int, float] = {}
        start_seconds = start // 1_000
        page_end = end // 1_000
        for _ in range(HISTORY_MAX_PAGES):
            payload = await self.transport.get(
                "https://api.kucoin.com/api/ua/v1/market/kline",
                {
                    "tradeType": "FUTURES",
                    "symbol": f"{instrument.symbol}-mark-price",
                    "interval": "5min",
                    "startAt": start_seconds,
                    "endAt": page_end,
                },
            )
            data = _kucoin_data(payload, "mark-price history", dict)
            candles = data.get("list")
            if not isinstance(candles, list):
                raise InvalidResponse("venue returned an invalid mark-price history")
            if not candles:
                return marks
            timestamps: list[int] = []
            for candle in candles:
                if not isinstance(candle, list) or len(candle) < 5:
                    raise InvalidResponse("venue changed the mark-price candle shape")
                timestamp = _integer_ms(number(candle[0]) * 1_000, "mark-price")
                timestamps.append(timestamp)
                marks[timestamp] = number(candle[4])
            oldest_seconds = min(timestamps) // 1_000
            if len(candles) < KUCOIN_HISTORY_LIMIT or oldest_seconds <= start_seconds:
                return marks
            advanced = oldest_seconds - 1
            if advanced >= page_end:
                raise PaginationError("mark-price history pagination did not advance")
            page_end = advanced
        raise PaginationError("mark-price history exceeded the bounded page limit")


class HtxAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "HTX"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        required = ("contract_direction",)
        if instrument.contract_direction is ContractDirection.INVERSE:
            required += ("contract_multiplier",)
        return OpenInterestCapabilities(True, True, 300, required_metadata=required)

    @staticmethod
    def _prefix(instrument: Instrument) -> str:
        if instrument.market_type == "future" and instrument.contract_direction is ContractDirection.INVERSE:
            return "api/v1"
        if instrument.contract_direction is ContractDirection.LINEAR:
            return "linear-swap-api/v1"
        if instrument.contract_direction is ContractDirection.INVERSE:
            return "swap-api/v1"
        raise InvalidInstrument("contract_direction is required for provider product routing")

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        prefix = self._prefix(instrument)
        future = instrument.market_type == "future"
        current_endpoint = (
            "contract_open_interest"
            if future and instrument.contract_direction is ContractDirection.INVERSE
            else "swap_open_interest"
        )
        current_params: dict[str, Any] = {"contract_code": instrument.symbol}
        if future and instrument.contract_direction is ContractDirection.LINEAR:
            current_params["business_type"] = "futures"
        payload = await self.transport.get(
            f"https://api.hbdm.com/{prefix}/{current_endpoint}",
            current_params,
        )
        rows, source_time = _htx_rows(payload, "open-interest snapshot")
        if len(rows) != 1:
            raise DataUnavailable("instrument does not resolve to one open-interest row")
        current = _htx_observation(instrument, rows[0], source_time)
        if not include_history:
            return OpenInterestResult(current)
        if future and instrument.contract_direction is ContractDirection.INVERSE:
            current_row = rows[0]
            if current_row.get("symbol") is None or current_row.get("contract_type") is None:
                return OpenInterestResult(
                    current,
                    history_issue=self._issue(
                        InvalidResponse(
                            "venue omitted the dated history identity from current open interest"
                        )
                    ),
                )
            try:
                hist = await self.transport.get(
                    "https://api.hbdm.com/api/v1/contract_his_open_interest",
                    {
                        "symbol": current_row["symbol"],
                        "contract_type": current_row["contract_type"],
                        "period": "60min",
                        "size": HTX_HISTORY_LIMIT,
                        "amount_type": 1,
                    },
                )
                history_rows = _htx_history_rows(hist, history)
                return OpenInterestResult(
                    current,
                    tuple(
                        _htx_observation(instrument, row, int(row["ts"]))
                        for row in history_rows
                    ),
                )
            except Exception as exc:
                return OpenInterestResult(current, history_issue=self._issue(exc))
        try:
            history_params: dict[str, Any] = {
                "contract_code": instrument.symbol,
                "period": "5min",
                "size": HTX_HISTORY_LIMIT,
                "amount_type": 1,
            }
            if future:
                history_params["business_type"] = "futures"
            hist = await self.transport.get(
                f"https://api.hbdm.com/{prefix}/swap_his_open_interest",
                history_params,
            )
            history_rows = _htx_history_rows(hist, history)
            return OpenInterestResult(
                current,
                tuple(_htx_observation(instrument, row, int(row["ts"])) for row in history_rows),
            )
        except Exception as exc:
            return OpenInterestResult(current, history_issue=self._issue(exc))


class ToobitAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "TOOBIT"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, False)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        oi_payload, mark = await asyncio.gather(
            self.transport.get(
                "https://api.toobit.com/quote/v1/openInterest",
                {"symbol": instrument.symbol},
            ),
            self.transport.get(
                "https://api.toobit.com/quote/v1/markPrice",
                {"symbol": instrument.symbol},
            ),
        )
        if not isinstance(oi_payload, dict) or not isinstance(
            oi_payload.get("openInterestList"), list
        ):
            raise InvalidResponse("venue returned an invalid open-interest snapshot")
        rows = oi_payload["openInterestList"]
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise DataUnavailable("instrument does not resolve to one open-interest row")
        if not isinstance(mark, dict):
            raise InvalidResponse("venue returned an invalid mark-price snapshot")
        row = rows[0]
        _require_identity(row, "symbol", instrument.symbol)
        _require_identity(mark, "symbolId", instrument.symbol)
        return OpenInterestResult(
            _base_observation(
                int(self.clock() * 1_000),
                row.get("size"),
                mark.get("price"),
                timestamp_kind=ObservationTimeKind.RETRIEVED,
            )
        )


class PhemexAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "PHEMEX"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        required = (
            ("contract_multiplier",)
            if instrument.contract_direction is ContractDirection.INVERSE
            else ()
        )
        return OpenInterestCapabilities(True, False, required_metadata=required)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        inverse = instrument.contract_direction is ContractDirection.INVERSE
        payload = await self.transport.get(
            f"https://api.phemex.com/md/v{'1' if inverse else '2'}/ticker/24hr",
            {"symbol": instrument.symbol},
        )
        if (
            not isinstance(payload, dict)
            or payload.get("error") is not None
            or not isinstance(payload.get("result"), dict)
        ):
            raise InvalidResponse("venue returned an invalid open-interest snapshot")
        row = payload["result"]
        _require_identity(row, "symbol", instrument.symbol)
        timestamp_ns = number(row.get("timestamp"))
        if timestamp_ns < 0 or not timestamp_ns.is_integer():
            raise InvalidResponse("venue returned an invalid open-interest source timestamp")
        timestamp = int(timestamp_ns) // 1_000_000
        if inverse:
            return OpenInterestResult(
                _inverse_contract_observation(
                    instrument, timestamp, row.get("openInterest")
                )
            )
        return OpenInterestResult(
            _base_observation(timestamp, row.get("openInterestRv"), row.get("markPriceRp"))
        )


class GrvtAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "GRVT"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, False)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        if instrument.market_type not in ("perpetual", "future"):
            raise InvalidInstrument("provider open interest supports futures contracts only")
        payload = await self.transport.post(
            "https://market-data.grvt.io/full/v1/ticker",
            {"instrument": instrument.symbol},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            raise InvalidResponse("venue returned an invalid open-interest snapshot")
        row = payload["result"]
        _require_identity(row, "instrument", instrument.symbol)
        timestamp_ns = number(row.get("event_time"))
        if timestamp_ns < 0 or not timestamp_ns.is_integer():
            raise InvalidResponse("venue returned an invalid open-interest source timestamp")
        return OpenInterestResult(
            _base_observation(
                int(timestamp_ns) // 1_000_000,
                row.get("open_interest"),
                row.get("mark_price"),
            )
        )


class LighterAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "LIGHTER"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, False)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        payload = await self.transport.get(
            "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails",
            {"market_id": instrument.symbol},
        )
        if (
            not isinstance(payload, dict)
            or payload.get("code") != 200
            or not isinstance(payload.get("order_book_details"), list)
        ):
            raise InvalidResponse("venue returned an invalid open-interest snapshot")
        rows = payload["order_book_details"]
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise DataUnavailable("instrument does not resolve to one open-interest row")
        row = rows[0]
        if str(row.get("market_id")) != instrument.symbol:
            raise InvalidResponse("venue returned a mismatched instrument identity")
        if row.get("market_type") != "perp":
            raise InvalidInstrument("provider market identity is not a perpetual contract")
        return OpenInterestResult(
            _base_observation(
                int(self.clock() * 1_000),
                row.get("open_interest"),
                row.get("mark_price"),
                timestamp_kind=ObservationTimeKind.RETRIEVED,
            )
        )


class BtseAdapter(NativeAdapter):
    BASE_URL = "https://api.btse.com/public-api/market/v1/ticker"

    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "BTSE"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(
            True, False, required_metadata=("contract_multiplier",)
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        ticker_payload, index_payload = await asyncio.gather(
            self.transport.get(
                f"{self.BASE_URL}/24hr", {"symbol": instrument.symbol}
            ),
            self.transport.get(
                f"{self.BASE_URL}/indices", {"symbol": instrument.symbol}
            ),
        )
        ticker_rows, timestamp = _btse_rows(ticker_payload, "open-interest snapshot")
        index_rows, _ = _btse_rows(index_payload, "mark-price snapshot")
        if len(ticker_rows) != 1 or len(index_rows) != 1:
            raise DataUnavailable("instrument does not resolve to one current market row")
        ticker, index = ticker_rows[0], index_rows[0]
        _require_identity(ticker, "symbol", instrument.symbol)
        _require_identity(index, "symbol", instrument.symbol)
        return OpenInterestResult(
            _contract_observation(
                instrument,
                timestamp,
                ticker.get("openInterest"),
                index.get("markPrice"),
            )
        )


class XtAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "XT"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(
            True, False, required_metadata=("contract_direction",)
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        if instrument.contract_direction is ContractDirection.LINEAR:
            host = "https://fapi.xt.com"
        elif instrument.contract_direction is ContractDirection.INVERSE:
            host = "https://dapi.xt.com"
        else:
            raise InvalidInstrument(
                "contract_direction is required for provider product routing"
            )
        payload = await self.transport.get(
            f"{host}/future/market/v1/public/contract/open-interest",
            {"symbol": instrument.symbol},
        )
        if (
            not isinstance(payload, dict)
            or str(payload.get("returnCode")) != "0"
            or not isinstance(payload.get("result"), dict)
        ):
            raise InvalidResponse("provider returned an invalid open-interest snapshot")
        row = payload["result"]
        _require_identity(row, "symbol", instrument.symbol)
        if row.get("openInterestUsd") in (None, "") or row.get("time") is None:
            raise DataUnavailable(
                "provider omitted open-interest notional or source time"
            )
        return OpenInterestResult(
            OpenInterestObservation(
                _integer_ms(row["time"], "open-interest"),
                number(row["openInterestUsd"]),
                valuation=ValuationMethod.VENUE_REPORTED,
            )
        )


class OkxAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "OKX"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, True, 300)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        inst_type = "FUTURES" if instrument.market_type == "future" else "SWAP"
        payload = await self.transport.get(
            "https://www.okx.com/api/v5/public/open-interest",
            {"instType": inst_type, "instId": instrument.symbol},
        )
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not rows:
            raise DataUnavailable("venue returned no open interest")
        row = rows[0]
        mark = number(row["markPx"]) if row.get("markPx") else None
        if row.get("oiUsd") not in (None, ""):
            value, valuation = number(row["oiUsd"]), ValuationMethod.VENUE_REPORTED
        elif row.get("oiCcy") not in (None, "") and mark is not None:
            value, valuation = number(row["oiCcy"]) * mark, ValuationMethod.MARK_PRICE
        else:
            raise DataUnavailable("venue omitted normalized open interest")
        native = number(row["oi"]) if row.get("oi") not in (None, "") else None
        base = (
            OpenInterestValueV1(
                Decimal(str(number(row["oiCcy"]))),
                OpenInterestMeasure.BASE_QUANTITY,
            )
            if row.get("oiCcy") not in (None, "")
            else None
        )
        current = OpenInterestObservation(
                int(row.get("ts") or self.clock() * 1000),
                value,
                native,
                NativeUnit.CONTRACTS if native is not None else None,
                mark,
                valuation,
                base,
                ObservationTimeKind.SOURCE
                if row.get("ts") is not None
                else ObservationTimeKind.RETRIEVED,
            )
        if not include_history:
            return OpenInterestResult(current)
        try:
            rows = await self._history(instrument, history)
            observations = tuple(
                OpenInterestObservation(
                    int(item[0]),
                    number(item[1]),
                    base_quantity=OpenInterestValueV1(
                        Decimal(str(number(item[2]))),
                        OpenInterestMeasure.BASE_QUANTITY,
                    ),
                )
                for item in rows
            )
            return OpenInterestResult(current, observations)
        except Exception as exc:
            return OpenInterestResult(current, history_issue=self._issue(exc))

    async def _history(
        self, instrument: Instrument, requested: HistoryRange | None
    ) -> list[list[Any]]:
        params: dict[str, Any] = {
            "instId": instrument.symbol,
            "period": "5m",
            "limit": 100,
        }
        if requested and requested.start_ms is not None:
            params["begin"] = requested.start_ms
        if requested and requested.end_ms is not None:
            params["end"] = requested.end_ms
        normalized: dict[int, list[Any]] = {}
        cursor_end = params.get("end")
        for _ in range(HISTORY_MAX_PAGES):
            request = {**params, **({"end": cursor_end} if cursor_end is not None else {})}
            payload = await self.transport.get(
                "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history",
                request,
            )
            if not isinstance(payload, dict) or str(payload.get("code")) != "0" or not isinstance(payload.get("data"), list):
                raise InvalidResponse("venue returned an invalid open-interest history")
            page = payload["data"]
            if any(not isinstance(item, list) or len(item) < 3 for item in page):
                raise InvalidResponse("venue returned an invalid open-interest history row")
            for item in page:
                timestamp = int(item[0])
                if requested and requested.start_ms is not None and timestamp < requested.start_ms:
                    continue
                if requested and requested.end_ms is not None and timestamp > requested.end_ms:
                    continue
                normalized[timestamp] = item
            if len(page) < 100:
                return [normalized[key] for key in sorted(normalized)]
            oldest = min(int(item[0]) for item in page)
            if requested and requested.start_ms is not None and oldest <= requested.start_ms:
                return [normalized[key] for key in sorted(normalized)]
            advanced = oldest - 1
            if cursor_end is not None and advanced >= cursor_end:
                raise PaginationError("open-interest history pagination did not advance")
            cursor_end = advanced
        raise PaginationError("open-interest history exceeded the bounded page limit")


class HyperliquidAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "HYPERLIQUID"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, False)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        scope, native_symbol = self._scope_and_symbol(instrument)
        request = {"type": "metaAndAssetCtxs"}
        if scope is not None:
            request["dex"] = scope
        payload = await self.transport.post("https://api.hyperliquid.xyz/info", request)
        if not isinstance(payload, list) or len(payload) != 2:
            raise InvalidResponse("venue returned invalid open interest")
        metadata, contexts = payload
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("universe"), list
        ):
            raise InvalidResponse("venue returned an invalid perpetual universe")
        universe = metadata["universe"]
        if not isinstance(contexts, list) or len(contexts) != len(universe):
            raise InvalidResponse(
                "venue returned misaligned perpetual metadata and contexts"
            )
        names: list[str] = []
        for row in universe:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("name"), str)
                or not row["name"]
            ):
                raise InvalidResponse(
                    "venue returned an invalid perpetual instrument identity"
                )
            names.append(row["name"])
        matches = [index for index, name in enumerate(names) if name == native_symbol]
        if len(matches) != 1:
            raise DataUnavailable(
                "instrument is missing or ambiguous in the venue perpetual universe"
            )
        context = contexts[matches[0]]
        if not isinstance(context, dict):
            raise InvalidResponse("venue returned an invalid perpetual asset context")
        if context.get("openInterest") is None or context.get("markPx") is None:
            raise InvalidResponse("venue omitted open interest or mark price")
        native, mark = number(context["openInterest"]), number(context["markPx"])
        return OpenInterestResult(
            OpenInterestObservation(
                int(self.clock() * 1000),
                native * mark,
                native,
                NativeUnit.BASE,
                mark,
                ValuationMethod.MARK_PRICE,
                proven_base_quantity(instrument, native, NativeUnit.BASE),
                ObservationTimeKind.RETRIEVED,
            )
        )

    @staticmethod
    def _scope_and_symbol(instrument: Instrument) -> tuple[str | None, str]:
        if isinstance(instrument, ReferenceInstrument):
            native_symbol = adapter_identity(
                instrument,
                RPC_INSTRUMENT,
                legacy_value=None,
            )
            scope = optional_adapter_identity(
                instrument,
                RPC_PRODUCT_FAMILY,
                legacy_value=None,
            )
            return scope, native_symbol
        parts = instrument.symbol.split(":")
        if len(parts) > 2 or any(not part for part in parts):
            raise InvalidInstrument(
                "venue-native symbol contains an invalid perpetual namespace"
            )
        symbol_scope = (
            HyperliquidAdapter._validate_scope(parts[0]) if len(parts) == 2 else None
        )
        product_scope = HyperliquidAdapter._product_scope(instrument.product)
        if (
            symbol_scope is not None
            and product_scope is not None
            and symbol_scope != product_scope
        ):
            raise InvalidInstrument(
                "venue-native symbol namespace and product scope disagree"
            )
        scope = symbol_scope or product_scope
        native_symbol = (
            instrument.symbol
            if symbol_scope is not None or scope is None
            else f"{scope}:{instrument.symbol}"
        )
        return scope, native_symbol

    @staticmethod
    def _product_scope(product: str | None) -> str | None:
        if product is None:
            return None
        if not isinstance(product, str) or not product or product != product.strip():
            raise InvalidInstrument("venue-native product descriptor is malformed")
        if product == HYPERLIQUID_SCOPED_PRODUCT_FAMILY:
            raise InvalidInstrument(
                "venue-native product descriptor omits its perpetual scope"
            )
        if ":" not in product:
            return None
        family, scope = product.split(":", 1)
        if family != HYPERLIQUID_SCOPED_PRODUCT_FAMILY:
            raise InvalidInstrument(
                "venue-native product descriptor uses an unsupported family"
            )
        return HyperliquidAdapter._validate_scope(scope)

    @staticmethod
    def _validate_scope(scope: str) -> str:
        if not scope or ":" in scope or any(character.isspace() for character in scope):
            raise InvalidInstrument("venue-native perpetual scope is malformed")
        return scope


class MexcAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "MEXC"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(
            True,
            False,
            required_metadata=("contract_direction", "contract_multiplier"),
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        payload = await self.transport.get(
            "https://contract.mexc.com/api/v1/contract/ticker"
        )
        if (
            not isinstance(payload, dict)
            or payload.get("success") is not True
            or payload.get("code") != 0
        ):
            raise InvalidResponse(
                "venue rejected the aggregate contract ticker request"
            )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise InvalidResponse("venue returned an invalid aggregate contract ticker")
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("symbol"), str)
            or not row["symbol"]
            for row in rows
        ):
            raise InvalidResponse(
                "venue returned an invalid aggregate contract ticker row"
            )

        matches = [row for row in rows if row["symbol"] == instrument.symbol]
        if len(matches) != 1:
            raise DataUnavailable(
                "instrument does not resolve to exactly one aggregate contract ticker row"
            )
        row = matches[0]
        contracts = number(row.get("holdVol"))
        mark = number(row.get("fairPrice"))
        if mark <= 0:
            raise InvalidResponse("venue returned a non-positive fair price")
        timestamp = number(row.get("timestamp"))
        if timestamp < 0 or not timestamp.is_integer():
            raise InvalidResponse("venue returned an invalid source timestamp")

        value = contract_value_usd(instrument, contracts, mark)
        valuation = (
            ValuationMethod.MARK_PRICE
            if instrument.contract_direction is ContractDirection.LINEAR
            else ValuationMethod.CONTRACT_VALUE
        )
        return OpenInterestResult(
            OpenInterestObservation(
                int(timestamp),
                value,
                contracts,
                NativeUnit.CONTRACTS,
                mark,
                valuation,
                proven_base_quantity(instrument, contracts, NativeUnit.CONTRACTS),
            )
        )


class KrakenAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "KRAKEN"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        required = ("contract_direction",)
        if instrument.contract_direction is ContractDirection.INVERSE:
            required = (*required, "contract_multiplier")
        return OpenInterestCapabilities(
            True,
            True,
            KRAKEN_HISTORY_INTERVAL_SECONDS,
            KRAKEN_HISTORY_DAYS,
            required,
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        direction = self._direction(instrument)
        if (
            direction is ContractDirection.INVERSE
            and instrument.contract_multiplier is None
        ):
            raise InvalidInstrument(
                "contract_multiplier is required for inverse contract-count open interest"
            )

        payload = await self.transport.get(KRAKEN_TICKERS_URL)
        row, timestamp_ms = self._current_row(payload, instrument.symbol)
        native = number(row.get("openInterest"))
        if native < 0:
            raise InvalidResponse("venue returned negative open interest")

        if direction is ContractDirection.LINEAR:
            mark = number(row.get("markPrice"))
            if mark <= 0:
                raise InvalidResponse("venue returned a non-positive mark price")
            value = native * mark
            native_unit = NativeUnit.BASE
            valuation = ValuationMethod.MARK_PRICE
        else:
            mark = None
            if row.get("markPrice") not in (None, ""):
                reported_mark = number(row["markPrice"])
                if reported_mark > 0:
                    mark = reported_mark
            value = contract_value_usd(instrument, native, mark)
            native_unit = NativeUnit.CONTRACTS
            valuation = ValuationMethod.CONTRACT_VALUE
        current = OpenInterestObservation(
            timestamp_ms,
            value,
            native,
            native_unit,
            mark,
            valuation,
            proven_base_quantity(instrument, native, native_unit),
        )
        if not include_history:
            return OpenInterestResult(current)

        try:
            observations, issue = await self._history(
                instrument,
                direction,
                history,
            )
            return OpenInterestResult(current, observations, issue)
        except Exception as exc:
            return OpenInterestResult(current, history_issue=self._issue(exc))

    async def _history(
        self,
        instrument: Instrument,
        direction: ContractDirection,
        requested: HistoryRange | None,
    ) -> tuple[tuple[OpenInterestObservation, ...], HistoryIssue | None]:
        bounds = self._history_bounds(requested)
        if bounds is None:
            return (), None
        start_ms, end_ms = bounds
        raw_history = await self._open_interest_history(
            instrument.symbol,
            start_ms,
            end_ms,
        )
        if direction is ContractDirection.INVERSE:
            multiplier = instrument.contract_multiplier
            if multiplier is None:
                raise InvalidInstrument(
                    "contract_multiplier is required for inverse contract-count open interest"
                )
            return (
                tuple(
                    OpenInterestObservation(
                        timestamp,
                        native * multiplier,
                        native,
                        NativeUnit.CONTRACTS,
                        valuation=ValuationMethod.CONTRACT_VALUE,
                        base_quantity=None,
                    )
                    for timestamp, native in sorted(raw_history.items())
                ),
                None,
            )

        marks = await self._mark_history(instrument.symbol, start_ms, end_ms)
        matched = sorted(raw_history.keys() & marks.keys())
        observations = tuple(
            OpenInterestObservation(
                timestamp,
                raw_history[timestamp] * marks[timestamp],
                raw_history[timestamp],
                NativeUnit.BASE,
                marks[timestamp],
                ValuationMethod.MARK_PRICE,
                proven_base_quantity(
                    instrument, raw_history[timestamp], NativeUnit.BASE
                ),
            )
            for timestamp in matched
        )
        missing = sorted(raw_history.keys() - marks.keys())
        if missing:
            return observations, HistoryIssue(
                "history_partial",
                "mark-price history omitted "
                f"{len(missing)} of {len(raw_history)} open-interest buckets",
            )
        return observations, None

    def _history_bounds(self, requested: HistoryRange | None) -> tuple[int, int] | None:
        current_bucket = (
            int(self.clock() * 1000) // HISTORY_BUCKET_MS * HISTORY_BUCKET_MS
        )
        latest_complete = current_bucket - HISTORY_BUCKET_MS
        if latest_complete < 0:
            return None
        available_start = max(
            0,
            latest_complete - KRAKEN_HISTORY_DAYS * 86_400_000 + HISTORY_BUCKET_MS,
        )
        start = max(
            available_start,
            requested.start_ms
            if requested is not None and requested.start_ms is not None
            else available_start,
        )
        requested_end = (
            requested.end_ms
            if requested is not None and requested.end_ms is not None
            else latest_complete
        )
        end = min(requested_end, latest_complete)
        if start > end:
            return None
        return start, end

    async def _open_interest_history(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> dict[int, float]:
        url = f"{KRAKEN_CHARTS_URL}/analytics/{quote(symbol, safe='')}/open-interest"
        next_since = start_ms // 1000
        end_seconds = end_ms // 1000
        rows: dict[int, float] = {}
        for _ in range(HISTORY_MAX_PAGES):
            payload = await self.transport.get(
                url,
                {
                    "since": next_since,
                    "to": end_seconds,
                    "interval": KRAKEN_HISTORY_INTERVAL_SECONDS,
                },
            )
            page, more = self._analytics_page(payload)
            for timestamp, value in page:
                timestamp_ms = timestamp * 1000
                if start_ms <= timestamp_ms <= end_ms:
                    rows[timestamp_ms] = value
            if not page:
                if more:
                    raise PaginationError(
                        "open-interest history requested another page without observations"
                    )
                return rows
            newest = page[-1][0]
            if newest >= end_seconds or not more:
                return rows
            advanced = newest + KRAKEN_HISTORY_INTERVAL_SECONDS
            if advanced <= next_since:
                raise PaginationError(
                    "open-interest history pagination did not advance"
                )
            next_since = advanced
        raise PaginationError("open-interest history exceeded the bounded page limit")

    async def _mark_history(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> dict[int, float]:
        url = f"{KRAKEN_CHARTS_URL}/mark/{quote(symbol, safe='')}/5m"
        next_from = start_ms // 1000
        end_seconds = end_ms // 1000
        rows: dict[int, float] = {}
        for _ in range(HISTORY_MAX_PAGES):
            payload = await self.transport.get(
                url,
                {"from": next_from, "to": end_seconds},
            )
            page, more = self._mark_page(payload)
            for timestamp_ms, value in page:
                if start_ms <= timestamp_ms <= end_ms:
                    rows[timestamp_ms] = value
            if not page:
                if more:
                    raise PaginationError(
                        "mark-price history requested another page without observations"
                    )
                return rows
            newest_ms = page[-1][0]
            if newest_ms >= end_ms or not more:
                return rows
            advanced = newest_ms // 1000 + KRAKEN_HISTORY_INTERVAL_SECONDS
            if advanced <= next_from:
                raise PaginationError("mark-price history pagination did not advance")
            next_from = advanced
        raise PaginationError("mark-price history exceeded the bounded page limit")

    @staticmethod
    def _current_row(payload: Any, symbol: str) -> tuple[dict[str, Any], int]:
        if not isinstance(payload, dict) or payload.get("result") != "success":
            raise InvalidResponse(
                "venue rejected the aggregate contract ticker request"
            )
        rows = payload.get("tickers")
        if not isinstance(rows, list):
            raise InvalidResponse("venue returned an invalid aggregate contract ticker")
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("symbol"), str)
            or not row["symbol"]
            for row in rows
        ):
            raise InvalidResponse(
                "venue returned an invalid aggregate contract ticker row"
            )
        matches = [row for row in rows if row["symbol"] == symbol]
        if len(matches) != 1:
            raise DataUnavailable(
                "instrument does not resolve to exactly one aggregate contract ticker row"
            )
        return matches[0], KrakenAdapter._server_time_ms(payload.get("serverTime"))

    @staticmethod
    def _server_time_ms(value: Any) -> int:
        if not isinstance(value, str) or not value:
            raise InvalidResponse("venue omitted the aggregate response timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidResponse(
                "venue returned an invalid aggregate response timestamp"
            ) from exc
        if parsed.tzinfo is None:
            raise InvalidResponse(
                "venue returned a timezone-naive aggregate response timestamp"
            )
        delta = parsed.astimezone(timezone.utc) - datetime(
            1970, 1, 1, tzinfo=timezone.utc
        )
        milliseconds = (
            delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000
        )
        if milliseconds < 0:
            raise InvalidResponse(
                "venue returned a negative aggregate response timestamp"
            )
        return milliseconds

    @staticmethod
    def _analytics_page(payload: Any) -> tuple[list[tuple[int, float]], bool]:
        if not isinstance(payload, dict) or payload.get("errors") != []:
            raise InvalidResponse("venue rejected the open-interest history request")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise InvalidResponse("venue returned an invalid open-interest history")
        timestamps = result.get("timestamp")
        values = result.get("data")
        more = result.get("more")
        if (
            not isinstance(timestamps, list)
            or not isinstance(values, list)
            or len(timestamps) != len(values)
            or not isinstance(more, bool)
        ):
            raise InvalidResponse("venue returned a misaligned open-interest history")
        page: list[tuple[int, float]] = []
        previous: int | None = None
        for raw_timestamp, candle in zip(timestamps, values):
            timestamp = KrakenAdapter._integer_timestamp(raw_timestamp, "open-interest")
            if previous is not None and timestamp <= previous:
                raise InvalidResponse("venue returned unordered open-interest history")
            if not isinstance(candle, list) or len(candle) != 4:
                raise InvalidResponse(
                    "venue changed the open-interest OHLC tuple shape"
                )
            ohlc = tuple(number(value) for value in candle)
            if any(value < 0 for value in ohlc):
                raise InvalidResponse("venue returned negative open-interest history")
            page.append((timestamp, ohlc[3]))
            previous = timestamp
        return page, more

    @staticmethod
    def _mark_page(payload: Any) -> tuple[list[tuple[int, float]], bool]:
        if not isinstance(payload, dict):
            raise InvalidResponse("venue returned an invalid mark-price history")
        candles = payload.get("candles")
        more = payload.get("more_candles")
        if not isinstance(candles, list) or not isinstance(more, bool):
            raise InvalidResponse("venue returned an invalid mark-price history")
        page: list[tuple[int, float]] = []
        previous: int | None = None
        for candle in candles:
            if not isinstance(candle, dict):
                raise InvalidResponse("venue returned an invalid mark-price candle")
            timestamp = KrakenAdapter._integer_timestamp(
                candle.get("time"),
                "mark-price",
            )
            if previous is not None and timestamp <= previous:
                raise InvalidResponse("venue returned unordered mark-price history")
            close = number(candle.get("close"))
            if close <= 0:
                raise InvalidResponse(
                    "venue returned a non-positive historical mark price"
                )
            page.append((timestamp, close))
            previous = timestamp
        return page, more

    @staticmethod
    def _integer_timestamp(value: Any, metric: str) -> int:
        timestamp = number(value)
        if timestamp < 0 or not timestamp.is_integer():
            raise InvalidResponse(
                f"venue returned an invalid {metric} source timestamp"
            )
        return int(timestamp)

    @staticmethod
    def _direction(instrument: Instrument) -> ContractDirection:
        direction = instrument.contract_direction
        if direction not in (ContractDirection.LINEAR, ContractDirection.INVERSE):
            raise InvalidInstrument(
                "contract_direction is required for mixed-unit open interest"
            )
        return direction


def native_adapters(transport: JsonTransport) -> dict[str, NativeAdapter]:
    adapters: list[NativeAdapter] = [
        BinanceAdapter(transport),
        BybitAdapter(transport),
        GateAdapter(transport),
        BitfinexAdapter(transport),
        DeepcoinAdapter(transport),
        KucoinAdapter(transport),
        HtxAdapter(transport),
        ToobitAdapter(transport),
        PhemexAdapter(transport),
        GrvtAdapter(transport),
        LighterAdapter(transport),
        BtseAdapter(transport),
        XtAdapter(transport),
        OkxAdapter(transport),
        HyperliquidAdapter(transport),
        MexcAdapter(transport),
        KrakenAdapter(transport),
    ]
    return {
        venue: adapter for adapter in adapters for venue in _supported_venues(adapter)
    }


def _supported_venues(adapter: NativeAdapter) -> tuple[str, ...]:
    return {
        BinanceAdapter: ("BINANCE",),
        BybitAdapter: ("BYBIT",),
        GateAdapter: ("GATE",),
        BitfinexAdapter: ("BITFINEX",),
        DeepcoinAdapter: ("DEEPCOIN",),
        KucoinAdapter: ("KUCOIN",),
        HtxAdapter: ("HTX",),
        ToobitAdapter: ("TOOBIT",),
        PhemexAdapter: ("PHEMEX",),
        GrvtAdapter: ("GRVT",),
        LighterAdapter: ("LIGHTER",),
        BtseAdapter: ("BTSE",),
        XtAdapter: ("XT",),
        OkxAdapter: ("OKX",),
        HyperliquidAdapter: ("HYPERLIQUID",),
        MexcAdapter: ("MEXC",),
        KrakenAdapter: ("KRAKEN",),
    }[type(adapter)]


def _integer_ms(value: Any, metric: str) -> int:
    parsed = number(value)
    if parsed < 0 or not parsed.is_integer():
        raise InvalidResponse(f"venue returned an invalid {metric} source timestamp")
    return int(parsed)


def _require_identity(row: dict[str, Any], field: str, expected: str) -> None:
    if row.get(field) != expected:
        raise InvalidResponse("venue returned a mismatched instrument identity")


def _btse_rows(payload: Any, description: str) -> tuple[list[dict[str, Any]], int]:
    if (
        not isinstance(payload, dict)
        or payload.get("success") is not True
        or not isinstance(payload.get("data"), (dict, list))
    ):
        raise InvalidResponse(f"venue returned an invalid {description}")
    data = payload["data"]
    rows = data if isinstance(data, list) else [data]
    if any(not isinstance(row, dict) for row in rows):
        raise InvalidResponse(f"venue returned an invalid {description}")
    return rows, _integer_ms(payload.get("time"), description)


def _base_observation(
    timestamp: int,
    raw_value: Any,
    mark_value: Any,
    *,
    timestamp_kind: ObservationTimeKind = ObservationTimeKind.SOURCE,
) -> OpenInterestObservation:
    native = number(raw_value)
    mark = number(mark_value)
    return OpenInterestObservation(
        timestamp,
        native * mark,
        native,
        NativeUnit.BASE,
        mark,
        ValuationMethod.MARK_PRICE,
        OpenInterestValueV1(
            Decimal(str(native)), OpenInterestMeasure.BASE_QUANTITY
        ),
        timestamp_kind,
    )


def _inverse_contract_observation(
    instrument: Instrument, timestamp: int, raw_value: Any
) -> OpenInterestObservation:
    contracts = number(raw_value)
    return OpenInterestObservation(
        timestamp,
        contract_value_usd(instrument, contracts, None),
        contracts,
        NativeUnit.CONTRACTS,
        valuation=ValuationMethod.CONTRACT_VALUE,
    )




def _coded_rows(
    payload: Any, description: str, *, row_type: type = dict
) -> list[Any]:
    if (
        not isinstance(payload, dict)
        or str(payload.get("code")) != "0"
        or not isinstance(payload.get("data"), list)
        or any(not isinstance(row, row_type) for row in payload["data"])
    ):
        raise InvalidResponse(f"venue returned an invalid {description}")
    return payload["data"]


def _kucoin_data(payload: Any, description: str, expected: type) -> Any:
    if (
        not isinstance(payload, dict)
        or str(payload.get("code")) != "200000"
        or not isinstance(payload.get("data"), expected)
    ):
        raise InvalidResponse(f"venue returned an invalid {description}")
    return payload["data"]


def _contract_observation(
    instrument: Instrument, timestamp: int, raw_value: Any, mark_value: Any
) -> OpenInterestObservation:
    raw, mark = number(raw_value), number(mark_value)
    if raw < 0 or mark <= 0:
        raise InvalidResponse("venue returned invalid open interest or mark price")
    return OpenInterestObservation(
        timestamp,
        contract_value_usd(instrument, raw, mark),
        raw,
        NativeUnit.CONTRACTS,
        mark,
        ValuationMethod.MARK_PRICE
        if instrument.contract_direction is ContractDirection.LINEAR
        else ValuationMethod.CONTRACT_VALUE,
        proven_base_quantity(instrument, raw, NativeUnit.CONTRACTS),
    )


def _join_contract_history(
    instrument: Instrument,
    rows: list[dict[str, Any]],
    marks: dict[int, float],
) -> tuple[tuple[OpenInterestObservation, ...], int]:
    points: list[OpenInterestObservation] = []
    missing = 0
    for row in rows:
        timestamp = _integer_ms(row.get("ts"), "open-interest")
        raw = row.get("oi", row.get("openInterest"))
        if instrument.contract_direction is ContractDirection.INVERSE:
            contracts = number(raw)
            points.append(
                OpenInterestObservation(
                    timestamp,
                    contract_value_usd(instrument, contracts, None),
                    contracts,
                    NativeUnit.CONTRACTS,
                    valuation=ValuationMethod.CONTRACT_VALUE,
                )
            )
            continue
        mark = marks.get(timestamp)
        if mark is None:
            missing += 1
            continue
        points.append(_contract_observation(instrument, timestamp, raw, mark))
    return tuple(points), missing


def _partial_mark_issue(missing: int, total: int) -> HistoryIssue | None:
    if not missing:
        return None
    return HistoryIssue(
        "history_partial",
        f"mark-price history omitted {missing} of {total} open-interest buckets",
    )


async def _backward_dict_history(
    transport: JsonTransport,
    url: str,
    base: dict[str, Any],
    timestamp_field: str,
    limit: int,
    requested: HistoryRange | None,
    *,
    coded: bool = False,
    kucoin: bool = False,
) -> list[dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    page_end = base.get("endTime", base.get("endAt"))
    for _ in range(HISTORY_MAX_PAGES):
        request = dict(base)
        if page_end is not None:
            request["endAt" if kucoin else "endTime"] = page_end
        payload = await transport.get(url, request)
        page = (
            _coded_rows(payload, "open-interest history")
            if coded
            else _kucoin_data(payload, "open-interest history", list)
        )
        if any(not isinstance(row, dict) for row in page):
            raise InvalidResponse("venue returned an invalid open-interest history row")
        if not page:
            return [rows[key] for key in sorted(rows)]
        timestamps = [_integer_ms(row.get(timestamp_field), "open-interest") for row in page]
        start = requested.start_ms if requested else None
        end = requested.end_ms if requested else None
        for timestamp, row in zip(timestamps, page):
            if (start is None or timestamp >= start) and (end is None or timestamp <= end):
                rows[timestamp] = row
        oldest = min(timestamps)
        if len(page) < limit or (start is not None and oldest <= start):
            return [rows[key] for key in sorted(rows)]
        advanced = oldest - 1
        if page_end is not None and advanced >= page_end:
            raise PaginationError("open-interest history pagination did not advance")
        page_end = advanced
    raise PaginationError("open-interest history exceeded the bounded page limit")


def _htx_rows(payload: Any, description: str) -> tuple[list[dict[str, Any]], int]:
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or not isinstance(payload.get("data"), list)
        or any(not isinstance(row, dict) for row in payload["data"])
    ):
        raise InvalidResponse(f"venue returned an invalid {description}")
    return payload["data"], _integer_ms(payload.get("ts"), description)


def _htx_observation(
    instrument: Instrument, row: dict[str, Any], timestamp: int
) -> OpenInterestObservation:
    contracts = number(row.get("volume"))
    if contracts < 0:
        raise InvalidResponse("venue returned negative open interest")
    amount = row.get("amount")
    value = row.get("value")
    if instrument.contract_direction is ContractDirection.LINEAR:
        if value is None:
            raise DataUnavailable("venue omitted normalized linear open interest")
        notional = number(value)
        base_amount = (
            number(amount)
            if amount is not None
            else contracts * number(instrument.contract_multiplier)
        )
        base_quantity = OpenInterestValueV1(
            Decimal(str(base_amount)), OpenInterestMeasure.BASE_QUANTITY
        )
        valuation = ValuationMethod.VENUE_REPORTED
    elif instrument.contract_direction is ContractDirection.INVERSE:
        notional = contract_value_usd(instrument, contracts, None)
        base_quantity = None
        valuation = ValuationMethod.CONTRACT_VALUE
    else:
        raise InvalidInstrument("contract_direction is required for provider product routing")
    return OpenInterestObservation(
        timestamp,
        notional,
        contracts,
        NativeUnit.CONTRACTS,
        valuation=valuation,
        base_quantity=base_quantity,
    )


def _htx_history_rows(
    payload: Any, requested: HistoryRange | None
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise InvalidResponse("venue rejected the open-interest history request")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("tick"), list):
        raise DataUnavailable("venue returned no open-interest history")
    rows: dict[int, dict[str, Any]] = {}
    for row in data["tick"]:
        if not isinstance(row, dict):
            raise InvalidResponse("venue returned an invalid open-interest history row")
        timestamp = _integer_ms(row.get("ts"), "open-interest")
        if requested is None or (
            (requested.start_ms is None or timestamp >= requested.start_ms)
            and (requested.end_ms is None or timestamp <= requested.end_ms)
        ):
            rows[timestamp] = row
    return [rows[key] for key in sorted(rows)]
