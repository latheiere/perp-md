from __future__ import annotations

from ._common import (
    Any,
    DataUnavailable,
    HISTORY_MAX_PAGES,
    HistoryRange,
    Instrument,
    InvalidResponse,
    KUCOIN_HISTORY_LIMIT,
    NativeAdapter,
    OpenInterestCapabilities,
    OpenInterestResult,
    PaginationError,
    _backward_dict_history,
    _contract_observation,
    _integer_ms,
    _join_contract_history,
    _kucoin_data,
    _partial_mark_issue,
    asyncio,
    number,
    quote,
)


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


