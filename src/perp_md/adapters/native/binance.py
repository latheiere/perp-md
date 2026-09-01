from __future__ import annotations

from ._common import (
    Any,
    BINANCE_HISTORY_DAYS,
    BINANCE_HISTORY_LIMIT,
    ContractDirection,
    HISTORY_BUCKET_MS,
    HISTORY_MAX_PAGES,
    HistoryRange,
    Instrument,
    InvalidInstrument,
    InvalidResponse,
    NativeAdapter,
    NativeUnit,
    ObservationTimeKind,
    OpenInterestCapabilities,
    OpenInterestObservation,
    OpenInterestResult,
    PaginationError,
    REST_PAIR,
    REST_PRODUCT_FAMILY,
    ValuationMethod,
    adapter_identity,
    asyncio,
    contract_value_usd,
    number,
    proven_base_quantity,
)


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


