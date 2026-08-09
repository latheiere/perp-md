from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from perp_md.errors import DataUnavailable, InvalidInstrument, InvalidResponse, PaginationError, PerpMdError
from perp_md.models import (
    ContractDirection,
    HistoryIssue,
    HistoryRange,
    Instrument,
    NativeUnit,
    OpenInterestCapabilities,
    OpenInterestObservation,
    OpenInterestResult,
    ValuationMethod,
)
from perp_md.normalization import contract_value_usd, number
from perp_md.transport import JsonTransport


BINANCE_HISTORY_LIMIT = 500
BYBIT_HISTORY_LIMIT = 200
GATE_HISTORY_LIMIT = 1_000
HISTORY_MAX_PAGES = 200
BINANCE_HISTORY_DAYS = 30
HISTORY_BUCKET_MS = 300_000
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
        return OpenInterestCapabilities(True, True, 300, BINANCE_HISTORY_DAYS, required)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        inverse = self._inverse(instrument)
        prefix = "https://dapi.binance.com/dapi/v1" if inverse else "https://fapi.binance.com/fapi/v1"
        history_url = "https://dapi.binance.com/futures/data/openInterestHist" if inverse else "https://fapi.binance.com/futures/data/openInterestHist"
        params: dict[str, Any] = {"period": "5m", "limit": BINANCE_HISTORY_LIMIT}
        if inverse:
            params.update({"pair": instrument.pair_symbol or instrument.symbol.removesuffix("_PERP"), "contractType": "PERPETUAL"})
        else:
            params["symbol"] = instrument.symbol
        oi, premium = await asyncio.gather(
            self.transport.get(f"{prefix}/openInterest", {"symbol": instrument.symbol}),
            self.transport.get(f"{prefix}/premiumIndex", {"symbol": instrument.symbol}),
        )
        raw = number(oi["openInterest"])
        mark = number(premium["markPrice"]) if premium.get("markPrice") is not None else None
        if inverse:
            value = contract_value_usd(instrument, raw, mark)
            valuation = ValuationMethod.CONTRACT_VALUE
        else:
            if mark is None:
                raise InvalidResponse("venue omitted mark price")
            value = raw * mark
            valuation = ValuationMethod.MARK_PRICE
        current = OpenInterestObservation(
            int(oi.get("time") or self.clock() * 1000),
            value,
            raw,
            NativeUnit.CONTRACTS,
            mark,
            valuation,
        )
        if not include_history:
            return OpenInterestResult(current)
        try:
            payload = await self._history(history_url, params, history)
            rows = tuple(
                OpenInterestObservation(
                    int(row["timestamp"]),
                    number(row["sumOpenInterest"]) * number(instrument.contract_multiplier)
                    if inverse
                    else number(row["sumOpenInterestValue"]),
                    valuation=ValuationMethod.CONTRACT_VALUE if inverse else ValuationMethod.VENUE_REPORTED,
                )
                for row in payload
            )
            return OpenInterestResult(current, rows)
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
        current_bucket = int(self.clock() * 1000) // HISTORY_BUCKET_MS * HISTORY_BUCKET_MS
        available_start = current_bucket - BINANCE_HISTORY_DAYS * 86_400_000 + HISTORY_BUCKET_MS
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
                raise PaginationError("open-interest history pagination did not advance")
            page_end = next_end
        raise PaginationError("open-interest history exceeded the bounded page limit")

    @staticmethod
    def _inverse(instrument: Instrument) -> bool:
        return instrument.contract_direction is ContractDirection.INVERSE or str(instrument.product or "").upper() == "COIN-M"


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
        category = "inverse" if instrument.contract_direction is ContractDirection.INVERSE else "linear"
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
        raw = number(row["openInterest"]) if row.get("openInterest") is not None else None
        if row.get("openInterestValue") is not None:
            value = number(row["openInterestValue"])
            valuation = ValuationMethod.VENUE_REPORTED
        elif raw is not None:
            value = raw if category == "inverse" else raw * mark
            valuation = ValuationMethod.CONTRACT_VALUE if category == "inverse" else ValuationMethod.MARK_PRICE
        else:
            raise DataUnavailable("venue omitted current open interest")
        current = OpenInterestObservation(
            int(ticker.get("time") or self.clock() * 1000),
            value,
            raw,
            NativeUnit.CONTRACTS,
            mark,
            valuation,
        )
        if not include_history:
            return OpenInterestResult(current)
        try:
            payload = await self._history(instrument, category, history)
            multiplier = 1 if category == "inverse" else mark
            observations = tuple(
                OpenInterestObservation(
                    int(item["timestamp"]),
                    number(item["openInterest"]) * multiplier,
                    valuation=ValuationMethod.CONTRACT_VALUE if category == "inverse" else ValuationMethod.CURRENT_MARK,
                )
                for item in payload
            )
            return OpenInterestResult(current, observations)
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
            current_bucket = int(self.clock() * 1000) // HISTORY_BUCKET_MS * HISTORY_BUCKET_MS
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
                if "startTime" not in params or params["startTime"] <= timestamp <= params["endTime"]:
                    rows[timestamp] = item
            next_cursor = result.get("nextPageCursor")
            if not next_cursor:
                return [rows[key] for key in sorted(rows)]
            if not isinstance(next_cursor, str) or next_cursor in seen:
                raise PaginationError("open-interest history returned an invalid cursor")
            seen.add(next_cursor)
            cursor = next_cursor
        raise PaginationError("open-interest history exceeded the bounded page limit")

    @staticmethod
    def _ok(payload: Any) -> None:
        if not isinstance(payload, dict) or str(payload.get("retCode")) != "0":
            raise InvalidResponse("venue rejected the request")


class GateAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "GATE"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, True, 300)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        settle = str(instrument.settlement_currency or "USDT").lower()
        details = await self.transport.get(
            f"https://api.gateio.ws/api/v4/futures/{settle}/contracts/{instrument.symbol}"
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
        current = OpenInterestObservation(
            int(self.clock() * 1000), value, native, NativeUnit.CONTRACTS, mark, valuation
        )
        if not include_history:
            return OpenInterestResult(current)
        try:
            payload = await self._history(settle, instrument.symbol, history)
            observations = tuple(
                OpenInterestObservation(
                    int(item["time"]) * 1000,
                    number(item["open_interest_usd"]),
                    valuation=ValuationMethod.VENUE_REPORTED,
                )
                for item in payload
            )
            return OpenInterestResult(current, observations)
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
        current_bucket = int(self.clock() * 1000) // HISTORY_BUCKET_MS * HISTORY_BUCKET_MS
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
                raise PaginationError("open-interest history pagination did not advance")
            next_from = advanced
        raise PaginationError("open-interest history exceeded the bounded page limit")


class BitfinexAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "BITFINEX"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, False, required_metadata=("contract_direction", "contract_multiplier"))

    async def fetch(self, instrument: Instrument, history: HistoryRange | None, *, include_history: bool) -> OpenInterestResult:
        payload = await self.transport.get(
            "https://api-pub.bitfinex.com/v2/status/deriv", {"keys": f"t{instrument.symbol}"}
        )
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], list):
            raise InvalidResponse("venue returned invalid derivative status")
        row = payload[0]
        if len(row) <= 18 or row[18] is None or row[15] is None or row[1] is None:
            raise DataUnavailable("venue omitted open interest, mark price, or timestamp")
        contracts, mark = number(row[18]), number(row[15])
        current = OpenInterestObservation(
            int(row[1]),
            contract_value_usd(instrument, contracts, mark),
            contracts,
            NativeUnit.CONTRACTS,
            mark,
            ValuationMethod.MARK_PRICE if instrument.contract_direction is ContractDirection.LINEAR else ValuationMethod.CONTRACT_VALUE,
        )
        return OpenInterestResult(current)


class OkxAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "OKX"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, False)

    async def fetch(self, instrument: Instrument, history: HistoryRange | None, *, include_history: bool) -> OpenInterestResult:
        payload = await self.transport.get(
            "https://www.okx.com/api/v5/public/open-interest",
            {"instType": "SWAP", "instId": instrument.symbol},
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
        return OpenInterestResult(OpenInterestObservation(
            int(row.get("ts") or self.clock() * 1000), value, native, NativeUnit.CONTRACTS if native is not None else None, mark, valuation
        ))


class HyperliquidAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "HYPERLIQUID"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, False)

    async def fetch(self, instrument: Instrument, history: HistoryRange | None, *, include_history: bool) -> OpenInterestResult:
        scope, native_symbol = self._scope_and_symbol(instrument)
        request = {"type": "metaAndAssetCtxs"}
        if scope is not None:
            request["dex"] = scope
        payload = await self.transport.post("https://api.hyperliquid.xyz/info", request)
        if not isinstance(payload, list) or len(payload) != 2:
            raise InvalidResponse("venue returned invalid open interest")
        metadata, contexts = payload
        if not isinstance(metadata, dict) or not isinstance(metadata.get("universe"), list):
            raise InvalidResponse("venue returned an invalid perpetual universe")
        universe = metadata["universe"]
        if not isinstance(contexts, list) or len(contexts) != len(universe):
            raise InvalidResponse("venue returned misaligned perpetual metadata and contexts")
        names: list[str] = []
        for row in universe:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
                raise InvalidResponse("venue returned an invalid perpetual instrument identity")
            names.append(row["name"])
        matches = [index for index, name in enumerate(names) if name == native_symbol]
        if len(matches) != 1:
            raise DataUnavailable("instrument is missing or ambiguous in the venue perpetual universe")
        context = contexts[matches[0]]
        if not isinstance(context, dict):
            raise InvalidResponse("venue returned an invalid perpetual asset context")
        if context.get("openInterest") is None or context.get("markPx") is None:
            raise InvalidResponse("venue omitted open interest or mark price")
        native, mark = number(context["openInterest"]), number(context["markPx"])
        return OpenInterestResult(OpenInterestObservation(
            int(self.clock() * 1000), native * mark, native, NativeUnit.BASE, mark, ValuationMethod.MARK_PRICE
        ))

    @staticmethod
    def _scope_and_symbol(instrument: Instrument) -> tuple[str | None, str]:
        parts = instrument.symbol.split(":")
        if len(parts) > 2 or any(not part for part in parts):
            raise InvalidInstrument("venue-native symbol contains an invalid perpetual namespace")
        symbol_scope = HyperliquidAdapter._validate_scope(parts[0]) if len(parts) == 2 else None
        product_scope = HyperliquidAdapter._product_scope(instrument.product)
        if symbol_scope is not None and product_scope is not None and symbol_scope != product_scope:
            raise InvalidInstrument("venue-native symbol namespace and product scope disagree")
        scope = symbol_scope or product_scope
        native_symbol = instrument.symbol if symbol_scope is not None or scope is None else f"{scope}:{instrument.symbol}"
        return scope, native_symbol

    @staticmethod
    def _product_scope(product: str | None) -> str | None:
        if product is None:
            return None
        if not isinstance(product, str) or not product or product != product.strip():
            raise InvalidInstrument("venue-native product descriptor is malformed")
        if product == HYPERLIQUID_SCOPED_PRODUCT_FAMILY:
            raise InvalidInstrument("venue-native product descriptor omits its perpetual scope")
        if ":" not in product:
            return None
        family, scope = product.split(":", 1)
        if family != HYPERLIQUID_SCOPED_PRODUCT_FAMILY:
            raise InvalidInstrument("venue-native product descriptor uses an unsupported family")
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
            raise InvalidResponse("venue rejected the aggregate contract ticker request")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise InvalidResponse("venue returned an invalid aggregate contract ticker")
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("symbol"), str)
            or not row["symbol"]
            for row in rows
        ):
            raise InvalidResponse("venue returned an invalid aggregate contract ticker row")

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
        if direction is ContractDirection.INVERSE and instrument.contract_multiplier is None:
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
        current_bucket = int(self.clock() * 1000) // HISTORY_BUCKET_MS * HISTORY_BUCKET_MS
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
        url = (
            f"{KRAKEN_CHARTS_URL}/analytics/"
            f"{quote(symbol, safe='')}/open-interest"
        )
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
                raise PaginationError("open-interest history pagination did not advance")
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
            raise InvalidResponse("venue rejected the aggregate contract ticker request")
        rows = payload.get("tickers")
        if not isinstance(rows, list):
            raise InvalidResponse("venue returned an invalid aggregate contract ticker")
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("symbol"), str)
            or not row["symbol"]
            for row in rows
        ):
            raise InvalidResponse("venue returned an invalid aggregate contract ticker row")
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
            raise InvalidResponse("venue returned an invalid aggregate response timestamp") from exc
        if parsed.tzinfo is None:
            raise InvalidResponse("venue returned a timezone-naive aggregate response timestamp")
        delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        milliseconds = (
            delta.days * 86_400_000
            + delta.seconds * 1000
            + delta.microseconds // 1000
        )
        if milliseconds < 0:
            raise InvalidResponse("venue returned a negative aggregate response timestamp")
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
                raise InvalidResponse("venue changed the open-interest OHLC tuple shape")
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
                raise InvalidResponse("venue returned a non-positive historical mark price")
            page.append((timestamp, close))
            previous = timestamp
        return page, more

    @staticmethod
    def _integer_timestamp(value: Any, metric: str) -> int:
        timestamp = number(value)
        if timestamp < 0 or not timestamp.is_integer():
            raise InvalidResponse(f"venue returned an invalid {metric} source timestamp")
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
        OkxAdapter(transport),
        HyperliquidAdapter(transport),
        MexcAdapter(transport),
        KrakenAdapter(transport),
    ]
    return {venue: adapter for adapter in adapters for venue in _supported_venues(adapter)}


def _supported_venues(adapter: NativeAdapter) -> tuple[str, ...]:
    return {
        BinanceAdapter: ("BINANCE",),
        BybitAdapter: ("BYBIT",),
        GateAdapter: ("GATE",),
        BitfinexAdapter: ("BITFINEX",),
        OkxAdapter: ("OKX",),
        HyperliquidAdapter: ("HYPERLIQUID",),
        MexcAdapter: ("MEXC",),
        KrakenAdapter: ("KRAKEN",),
    }[type(adapter)]
