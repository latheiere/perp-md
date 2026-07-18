from __future__ import annotations

import asyncio
import importlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from perp_md.errors import AdapterUnavailable, DataUnavailable, InvalidResponse, PerpMdError, RequestError
from perp_md.models import (
    HistoryIssue,
    HistoryRange,
    Instrument,
    NativeUnit,
    OpenInterestCapabilities,
    OpenInterestObservation,
    OpenInterestResult,
    ValuationMethod,
)
from perp_md.normalization import contract_value_usd, number, verify_multiplier


DEFAULT_EXCHANGE_IDS = {
    "BITFINEX": "bitfinex",
    "BITGET": "bitget",
    "BITMART": "bitmart",
    "COINBASE": "coinbaseinternational",
    "DERIBIT": "deribit",
    "HTX": "htx",
    "KUCOIN": "kucoin",
    "MEXC": "mexc",
    "WHITEBIT": "whitebit",
    "XT": "xt",
}


@dataclass
class CcxtAdapter:
    timeout_seconds: float = 10
    exchange_ids: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EXCHANGE_IDS))
    exchanges: dict[str, Any] = field(default_factory=dict, init=False)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)

    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue in self.exchange_ids

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        required = ("contract_direction", "contract_multiplier") if instrument.venue in {"BITFINEX", "BITGET", "COINBASE", "WHITEBIT"} else ()
        return OpenInterestCapabilities(True, instrument.venue in {"HTX", "OKX"}, 300, required_metadata=required)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        try:
            if instrument.venue == "COINBASE":
                return await self._coinbase(instrument)
            if instrument.venue == "WHITEBIT":
                return await self._whitebit(instrument)
            exchange, symbol = await self._market(instrument)
            if not exchange.has.get("fetchOpenInterest"):
                raise DataUnavailable("open interest is not available for this venue")
            payload = await exchange.fetch_open_interest(symbol)
            native = payload.get("openInterestAmount")
            mark: float | None = None
            if payload.get("openInterestValue") is not None:
                value = number(payload["openInterestValue"])
                valuation = ValuationMethod.VENUE_REPORTED
            elif native is not None:
                mark = self._mark(await exchange.fetch_ticker(symbol))
                value = contract_value_usd(instrument, number(native), mark)
                valuation = ValuationMethod.MARK_PRICE
            else:
                raise DataUnavailable("venue omitted open-interest amount and normalized value")
            current = OpenInterestObservation(
                int(payload.get("timestamp") or time.time() * 1000),
                value,
                number(native) if native is not None else None,
                NativeUnit.CONTRACTS if native is not None else None,
                mark,
                valuation,
            )
            if not include_history or not exchange.has.get("fetchOpenInterestHistory"):
                return OpenInterestResult(current)
            try:
                rows = await exchange.fetch_open_interest_history(
                    symbol,
                    timeframe="5m",
                    since=history.start_ms if history else None,
                    limit=100,
                )
                end = history.end_ms if history else None
                observations = tuple(
                    OpenInterestObservation(
                        int(row["timestamp"]),
                        number(row["openInterestValue"]),
                        valuation=ValuationMethod.VENUE_REPORTED,
                    )
                    for row in rows
                    if row.get("timestamp") is not None
                    and row.get("openInterestValue") is not None
                    and (end is None or int(row["timestamp"]) <= end)
                )
                return OpenInterestResult(current, tuple(sorted(observations, key=lambda row: row.timestamp_ms)))
            except Exception as exc:
                return OpenInterestResult(current, history_issue=HistoryIssue(
                    "history_unavailable", self._summary(exc)
                ))
        except PerpMdError:
            raise
        except Exception as exc:
            raise RequestError("venue adapter request failed") from exc

    async def close(self) -> None:
        exchanges = list(self.exchanges.values())
        self.exchanges.clear()
        await asyncio.gather(*(exchange.close() for exchange in exchanges), return_exceptions=True)

    async def _coinbase(self, instrument: Instrument) -> OpenInterestResult:
        exchange, _ = await self._market(instrument)
        payload = await exchange.v1_public_get_instruments()
        if not isinstance(payload, list):
            raise InvalidResponse("venue returned an invalid instrument catalog")
        target = instrument.pair_symbol or instrument.symbol.removesuffix("-INTX")
        rows = [row for row in payload if str(row.get("symbol", "")).upper() == target.upper()]
        if len(rows) != 1:
            raise DataUnavailable("instrument is missing or ambiguous in the venue catalog")
        row = rows[0]
        quote = row.get("quote") or {}
        if row.get("open_interest") is None or quote.get("mark_price") is None:
            raise DataUnavailable("venue omitted open interest or mark price")
        verify_multiplier(instrument, row.get("base_asset_multiplier"))
        native, mark = number(row["open_interest"]), number(quote["mark_price"])
        return OpenInterestResult(OpenInterestObservation(
            self._iso_ms(quote.get("timestamp")),
            contract_value_usd(instrument, native, mark),
            native,
            NativeUnit.CONTRACTS,
            mark,
            ValuationMethod.MARK_PRICE,
        ))

    async def _whitebit(self, instrument: Instrument) -> OpenInterestResult:
        exchange, _ = await self._market(instrument)
        payload = await exchange.v4_public_get_futures()
        if not isinstance(payload, dict) or not payload.get("success") or not isinstance(payload.get("result"), list):
            raise InvalidResponse("venue returned an invalid futures catalog")
        rows = [row for row in payload["result"] if str(row.get("ticker_id", "")).upper() == instrument.symbol.upper()]
        if len(rows) != 1:
            raise DataUnavailable("instrument is missing or ambiguous in the venue catalog")
        row = rows[0]
        mark_raw = row.get("index_price") or row.get("last_price")
        if row.get("open_interest") is None or mark_raw is None:
            raise DataUnavailable("venue omitted open interest or reference price")
        native, mark = number(row["open_interest"]), number(mark_raw)
        return OpenInterestResult(OpenInterestObservation(
            int(time.time() * 1000),
            contract_value_usd(instrument, native, mark),
            native,
            NativeUnit.CONTRACTS,
            mark,
            ValuationMethod.MARK_PRICE,
        ))

    async def _market(self, instrument: Instrument) -> tuple[Any, str]:
        exchange_id = self.exchange_ids.get(instrument.venue)
        try:
            ccxt = importlib.import_module("ccxt.async_support")
        except ImportError as exc:
            raise AdapterUnavailable("optional CCXT adapter is not installed") from exc
        if not exchange_id or not hasattr(ccxt, exchange_id):
            raise AdapterUnavailable("no CCXT adapter is configured for this venue")
        async with self.locks.setdefault(instrument.venue, asyncio.Lock()):
            exchange = self.exchanges.get(instrument.venue)
            if exchange is None:
                exchange = getattr(ccxt, exchange_id)({
                    "enableRateLimit": True,
                    "timeout": int(self.timeout_seconds * 1000),
                })
                try:
                    await exchange.load_markets()
                except Exception as exc:
                    await exchange.close()
                    raise RequestError("venue market catalog failed") from exc
                self.exchanges[instrument.venue] = exchange
        return exchange, resolve_ccxt_symbol(exchange, instrument)

    @staticmethod
    def _mark(ticker: dict[str, Any]) -> float:
        info = ticker.get("info") if isinstance(ticker.get("info"), dict) else {}
        raw = ticker.get("mark") or info.get("markPrice") or info.get("mark_price") or ticker.get("last")
        if raw is None:
            raise DataUnavailable("venue omitted mark and last price")
        return number(raw)

    @staticmethod
    def _iso_ms(raw: Any) -> int:
        if raw in (None, ""):
            return int(time.time() * 1000)
        try:
            return int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError as exc:
            raise InvalidResponse("venue returned an invalid observation timestamp") from exc

    @staticmethod
    def _summary(exc: Exception) -> str:
        detail = str(exc).strip()
        return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def resolve_ccxt_symbol(exchange: Any, instrument: Instrument) -> str:
    raw = instrument.symbol
    candidates = exchange.markets_by_id.get(raw, [])
    if not candidates and instrument.venue == "COINBASE":
        candidates = exchange.markets_by_id.get(raw.removesuffix("-INTX"), [])
    if not candidates and instrument.venue == "BITFINEX":
        candidates = exchange.markets_by_id.get(f"t{raw}", [])
    if isinstance(candidates, dict):
        candidates = [candidates]
    contracts = [row for row in candidates if row.get("contract")]
    if len(contracts) == 1:
        return contracts[0]["symbol"]
    matches = [
        row
        for key, values in exchange.markets_by_id.items()
        if str(key).upper() == raw.upper()
        for row in (values if isinstance(values, list) else [values])
        if row.get("contract")
    ]
    if len(matches) == 1:
        return matches[0]["symbol"]
    raise DataUnavailable("venue-native instrument is not uniquely exposed by CCXT")
