from __future__ import annotations

import asyncio
import importlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from perp_md.capabilities import (
    CCXT_OPEN_INTEREST_FEATURE,
    CCXT_OPEN_INTEREST_HISTORY_FEATURE,
    CCXT_SPECIALIZED_OPEN_INTEREST_FEATURE,
)
from perp_md.errors import (
    AdapterUnavailable,
    DataUnavailable,
    InvalidResponse,
    PerpMdError,
    RequestError,
)
from perp_md.identity import (
    REST_INSTRUMENT,
    REST_INSTRUMENT_CATALOG_INSTRUMENT,
    ReferenceInstrument,
    adapter_identity,
)
from perp_md.models import (
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
from perp_md.normalization import (
    contract_value_usd,
    number,
    proven_base_quantity,
    verify_multiplier,
)

DEFAULT_EXCHANGE_IDS = {
    "ASTER": "aster",
    "BITFINEX": "bitfinex",
    "BITGET": "bitget",
    "BITMART": "bitmart",
    "BINGX": "bingx",
    "BLOFIN": "blofin",
    "COINBASE": "coinbaseinternational",
    "DERIBIT": "deribit",
    "DIGIFINEX": "digifinex",
    "HTX": "htx",
    "KUCOIN": "kucoin",
    "MEXC": "mexc",
    "CRYPTOCOM": "cryptocom",
    "WHITEBIT": "whitebit",
    "WEEX": "weex",
    "XT": "xt",
}

HTX_HISTORY_INTERVAL_SECONDS = 3_600
HTX_HISTORY_LIMIT = 200
HTX_MAX_HISTORY_DAYS = 8


@dataclass
class CcxtAdapter:
    timeout_seconds: float = 10
    exchange_ids: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_EXCHANGE_IDS)
    )
    exchanges: dict[str, Any] = field(default_factory=dict, init=False)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)

    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue in self.exchange_ids

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        if instrument.venue == "WHITEBIT":
            return OpenInterestCapabilities(False, False)
        required = (
            ("contract_direction", "contract_multiplier")
            if instrument.venue in {"BITFINEX", "BITGET", "COINBASE"}
            else ()
        )
        if instrument.venue == "HTX":
            return OpenInterestCapabilities(
                True,
                True,
                HTX_HISTORY_INTERVAL_SECONDS,
                HTX_MAX_HISTORY_DAYS,
                required,
            )
        if instrument.venue == "OKX":
            return OpenInterestCapabilities(True, True, 300, required_metadata=required)
        return OpenInterestCapabilities(True, False, required_metadata=required)

    async def runtime_features(self, instrument: Instrument) -> frozenset[str]:
        """Probe exact optional-runtime methods without loading a market catalog."""

        try:
            exchange, owned = self._runtime_exchange(instrument)
        except AdapterUnavailable:
            return frozenset()
        try:
            features: set[str] = set()
            declared = getattr(exchange, "has", None)
            supported = declared if isinstance(declared, dict) else {}
            if bool(supported.get("fetchOpenInterest")):
                features.add(CCXT_OPEN_INTEREST_FEATURE)
            if bool(supported.get("fetchOpenInterestHistory")):
                features.add(CCXT_OPEN_INTEREST_HISTORY_FEATURE)
            if (
                instrument.venue == "COINBASE"
                and callable(getattr(exchange, "v1_public_get_instruments", None))
            ):
                features.add(CCXT_SPECIALIZED_OPEN_INTEREST_FEATURE)
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
    ) -> OpenInterestResult:
        try:
            if instrument.venue == "COINBASE":
                return await self._coinbase(instrument)
            if not self.capabilities(instrument).current:
                raise DataUnavailable("open interest is not supported for this venue")
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
                raise DataUnavailable(
                    "venue omitted open-interest amount and normalized value"
                )
            current = OpenInterestObservation(
                int(payload.get("timestamp") or time.time() * 1000),
                value,
                number(native) if native is not None else None,
                NativeUnit.CONTRACTS if native is not None else None,
                mark,
                valuation,
                proven_base_quantity(
                    instrument,
                    number(native),
                    NativeUnit.CONTRACTS,
                )
                if native is not None
                else None,
                ObservationTimeKind.SOURCE
                if payload.get("timestamp") is not None
                else ObservationTimeKind.RETRIEVED,
            )
            if not include_history:
                return OpenInterestResult(current)
            if not exchange.has.get("fetchOpenInterestHistory"):
                if self.capabilities(instrument).history:
                    return OpenInterestResult(
                        current,
                        history_issue=HistoryIssue(
                            "history_unavailable",
                            "venue runtime does not expose open-interest history",
                        ),
                    )
                return OpenInterestResult(current)
            try:
                timeframe = "1h" if instrument.venue == "HTX" else "5m"
                limit = HTX_HISTORY_LIMIT if instrument.venue == "HTX" else 100
                rows = await exchange.fetch_open_interest_history(
                    symbol,
                    timeframe=timeframe,
                    since=history.start_ms if history else None,
                    limit=limit,
                )
                if not isinstance(rows, list):
                    raise InvalidResponse(
                        "venue returned an invalid open-interest history"
                    )
                start = history.start_ms if history else None
                end = history.end_ms if history else None
                observations: dict[int, OpenInterestObservation] = {}
                for row in rows:
                    if not isinstance(row, dict):
                        raise InvalidResponse(
                            "venue returned an invalid open-interest history row"
                        )
                    if (
                        row.get("timestamp") is None
                        or row.get("openInterestValue") is None
                    ):
                        raise InvalidResponse(
                            "venue omitted a required open-interest history field"
                        )
                    timestamp = int(row["timestamp"])
                    if (start is None or timestamp >= start) and (
                        end is None or timestamp <= end
                    ):
                        observations[timestamp] = OpenInterestObservation(
                            timestamp,
                            number(row["openInterestValue"]),
                            valuation=ValuationMethod.VENUE_REPORTED,
                        )
                return OpenInterestResult(
                    current,
                    tuple(
                        sorted(observations.values(), key=lambda row: row.timestamp_ms)
                    ),
                )
            except Exception as exc:
                return OpenInterestResult(
                    current,
                    history_issue=HistoryIssue(
                        "history_unavailable", self._summary(exc)
                    ),
                )
        except PerpMdError:
            raise
        except Exception as exc:
            raise RequestError("venue adapter request failed") from exc

    async def close(self) -> None:
        exchanges = list(self.exchanges.values())
        self.exchanges.clear()
        await asyncio.gather(
            *(exchange.close() for exchange in exchanges), return_exceptions=True
        )

    async def _coinbase(self, instrument: Instrument) -> OpenInterestResult:
        exchange, owned = self._runtime_exchange(instrument)
        try:
            payload = await exchange.v1_public_get_instruments()
        finally:
            if owned:
                await exchange.close()
        if not isinstance(payload, list):
            raise InvalidResponse("venue returned an invalid instrument catalog")
        legacy_target = (
            instrument.pair_symbol or instrument.symbol
            if isinstance(instrument, Instrument)
            else None
        )
        target = adapter_identity(
            instrument,
            REST_INSTRUMENT_CATALOG_INSTRUMENT,
            legacy_value=legacy_target,
        )
        rows = [row for row in payload if row.get("symbol") == target]
        if len(rows) != 1:
            raise DataUnavailable(
                "instrument is missing or ambiguous in the venue catalog"
            )
        row = rows[0]
        quote = row.get("quote") or {}
        if row.get("open_interest") is None or quote.get("mark_price") is None:
            raise DataUnavailable("venue omitted open interest or mark price")
        verify_multiplier(instrument, row.get("base_asset_multiplier"))
        native, mark = number(row["open_interest"]), number(quote["mark_price"])
        return OpenInterestResult(
            OpenInterestObservation(
                self._iso_ms(quote.get("timestamp")),
                contract_value_usd(instrument, native, mark),
                native,
                NativeUnit.CONTRACTS,
                mark,
                ValuationMethod.MARK_PRICE,
                proven_base_quantity(instrument, native, NativeUnit.CONTRACTS),
            )
        )

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
                exchange = getattr(ccxt, exchange_id)(
                    {
                        "enableRateLimit": True,
                        "timeout": int(self.timeout_seconds * 1000),
                    }
                )
                try:
                    await exchange.load_markets()
                except asyncio.CancelledError:
                    await exchange.close()
                    raise
                except Exception as exc:
                    await exchange.close()
                    raise RequestError("venue market catalog failed") from exc
                self.exchanges[instrument.venue] = exchange
        return exchange, resolve_ccxt_symbol(exchange, instrument)

    def _runtime_exchange(self, instrument: Instrument) -> tuple[Any, bool]:
        exchange = self.exchanges.get(instrument.venue)
        if exchange is not None:
            return exchange, False
        exchange_id = self.exchange_ids.get(instrument.venue)
        try:
            ccxt = importlib.import_module("ccxt.async_support")
        except ImportError as exc:
            raise AdapterUnavailable("optional CCXT adapter is not installed") from exc
        if not exchange_id or not hasattr(ccxt, exchange_id):
            raise AdapterUnavailable("no CCXT adapter is configured for this venue")
        return getattr(ccxt, exchange_id)(
            {
                "enableRateLimit": True,
                "timeout": int(self.timeout_seconds * 1000),
            }
        ), True

    @staticmethod
    def _mark(ticker: dict[str, Any]) -> float:
        info = ticker.get("info") if isinstance(ticker.get("info"), dict) else {}
        raw = (
            ticker.get("mark")
            or info.get("markPrice")
            or info.get("mark_price")
            or ticker.get("last")
        )
        if raw is None:
            raise DataUnavailable("venue omitted mark and last price")
        return number(raw)

    @staticmethod
    def _iso_ms(raw: Any) -> int:
        if raw in (None, ""):
            return int(time.time() * 1000)
        try:
            return int(
                datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
                * 1000
            )
        except ValueError as exc:
            raise InvalidResponse(
                "venue returned an invalid observation timestamp"
            ) from exc

    @staticmethod
    def _summary(exc: Exception) -> str:
        detail = str(exc).strip()
        return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def resolve_ccxt_symbol(exchange: Any, instrument: Instrument) -> str:
    if isinstance(instrument, ReferenceInstrument):
        identities = (
            adapter_identity(
                instrument,
                REST_INSTRUMENT,
                legacy_value=None,
            ),
        )
    else:
        identities = tuple(
            dict.fromkeys(
                value
                for value in (instrument.symbol, instrument.pair_symbol)
                if value is not None
            )
        )
    matches: list[dict[str, Any]] = []
    for identity in identities:
        candidates = exchange.markets_by_id.get(identity, [])
        if isinstance(candidates, dict):
            candidates = [candidates]
        matches.extend(row for row in candidates if row.get("contract"))
    unique = {
        str(row.get("symbol")): row
        for row in matches
        if isinstance(row.get("symbol"), str) and row["symbol"]
    }
    if len(unique) == 1:
        return next(iter(unique))
    raise DataUnavailable("venue-native instrument is not uniquely exposed by CCXT")
