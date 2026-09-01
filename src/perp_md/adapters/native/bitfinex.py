from __future__ import annotations

from ._common import (
    Any,
    BITFINEX_HISTORY_LIMIT,
    ContractDirection,
    DataUnavailable,
    HISTORY_MAX_PAGES,
    HistoryRange,
    Instrument,
    InvalidResponse,
    NativeAdapter,
    NativeUnit,
    OpenInterestCapabilities,
    OpenInterestObservation,
    OpenInterestResult,
    PaginationError,
    REST_DERIVATIVE_STATUS_INSTRUMENT,
    ValuationMethod,
    _integer_ms,
    adapter_identity,
    contract_value_usd,
    number,
    proven_base_quantity,
    quote,
)


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


