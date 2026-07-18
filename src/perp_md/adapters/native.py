from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from perp_md.errors import DataUnavailable, InvalidResponse, PaginationError, PerpMdError
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
            if len(page) < int(base["limit"]) or newest * 1000 >= end:
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
        payload = await self.transport.post("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"})
        if not isinstance(payload, list) or len(payload) != 2:
            raise InvalidResponse("venue returned invalid open interest")
        names = [str(row.get("name", "")) for row in payload[0].get("universe", [])]
        try:
            context = payload[1][names.index(instrument.symbol)]
        except (ValueError, IndexError) as exc:
            raise DataUnavailable("instrument is absent from the venue perpetual universe") from exc
        native, mark = number(context["openInterest"]), number(context["markPx"])
        return OpenInterestResult(OpenInterestObservation(
            int(self.clock() * 1000), native * mark, native, NativeUnit.BASE, mark, ValuationMethod.MARK_PRICE
        ))


def native_adapters(transport: JsonTransport) -> dict[str, NativeAdapter]:
    adapters: list[NativeAdapter] = [
        BinanceAdapter(transport),
        BybitAdapter(transport),
        GateAdapter(transport),
        BitfinexAdapter(transport),
        OkxAdapter(transport),
        HyperliquidAdapter(transport),
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
    }[type(adapter)]
