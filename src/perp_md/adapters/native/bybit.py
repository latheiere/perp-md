from __future__ import annotations

from ._common import (
    Any,
    BYBIT_HISTORY_LIMIT,
    BYBIT_MARK_HISTORY_LIMIT,
    ContractDirection,
    DataUnavailable,
    Decimal,
    HISTORY_BUCKET_MS,
    HISTORY_MAX_PAGES,
    HistoryIssue,
    HistoryRange,
    Instrument,
    InvalidResponse,
    NativeAdapter,
    NativeUnit,
    ObservationTimeKind,
    OpenInterestCapabilities,
    OpenInterestMeasure,
    OpenInterestObservation,
    OpenInterestResult,
    OpenInterestValueV1,
    PaginationError,
    ValuationMethod,
    number,
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


