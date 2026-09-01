from __future__ import annotations

from ._common import (
    Any,
    ContractDirection,
    DataUnavailable,
    HISTORY_BUCKET_MS,
    HISTORY_MAX_PAGES,
    HistoryIssue,
    HistoryRange,
    Instrument,
    InvalidInstrument,
    InvalidResponse,
    KRAKEN_CHARTS_URL,
    KRAKEN_HISTORY_DAYS,
    KRAKEN_HISTORY_INTERVAL_SECONDS,
    KRAKEN_TICKERS_URL,
    NativeAdapter,
    NativeUnit,
    OpenInterestCapabilities,
    OpenInterestObservation,
    OpenInterestResult,
    PaginationError,
    ValuationMethod,
    contract_value_usd,
    datetime,
    number,
    proven_base_quantity,
    quote,
    timezone,
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


