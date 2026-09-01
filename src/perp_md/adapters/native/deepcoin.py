from __future__ import annotations

from ._common import (
    Any,
    DEEPCOIN_HISTORY_LIMIT,
    DataUnavailable,
    HISTORY_MAX_PAGES,
    HistoryRange,
    Instrument,
    InvalidResponse,
    NativeAdapter,
    OpenInterestCapabilities,
    OpenInterestResult,
    PaginationError,
    _backward_dict_history,
    _coded_rows,
    _contract_observation,
    _integer_ms,
    _join_contract_history,
    _partial_mark_issue,
    asyncio,
    number,
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
            stop_on_short_page=requested is None or requested.start_ms is None,
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


