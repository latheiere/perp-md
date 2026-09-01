from __future__ import annotations

from ._common import (
    Any,
    DataUnavailable,
    Decimal,
    HISTORY_MAX_PAGES,
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


class OkxAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "OKX"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, True, 300)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        inst_type = "FUTURES" if instrument.market_type == "future" else "SWAP"
        payload = await self.transport.get(
            "https://www.okx.com/api/v5/public/open-interest",
            {"instType": inst_type, "instId": instrument.symbol},
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
        base = (
            OpenInterestValueV1(
                Decimal(str(number(row["oiCcy"]))),
                OpenInterestMeasure.BASE_QUANTITY,
            )
            if row.get("oiCcy") not in (None, "")
            else None
        )
        current = OpenInterestObservation(
                int(row.get("ts") or self.clock() * 1000),
                value,
                native,
                NativeUnit.CONTRACTS if native is not None else None,
                mark,
                valuation,
                base,
                ObservationTimeKind.SOURCE
                if row.get("ts") is not None
                else ObservationTimeKind.RETRIEVED,
            )
        if not include_history:
            return OpenInterestResult(current)
        try:
            rows = await self._history(instrument, history)
            observations = tuple(
                OpenInterestObservation(
                    int(item[0]),
                    number(item[1]),
                    base_quantity=OpenInterestValueV1(
                        Decimal(str(number(item[2]))),
                        OpenInterestMeasure.BASE_QUANTITY,
                    ),
                )
                for item in rows
            )
            return OpenInterestResult(current, observations)
        except Exception as exc:
            return OpenInterestResult(current, history_issue=self._issue(exc))

    async def _history(
        self, instrument: Instrument, requested: HistoryRange | None
    ) -> list[list[Any]]:
        params: dict[str, Any] = {
            "instId": instrument.symbol,
            "period": "5m",
            "limit": 100,
        }
        if requested and requested.start_ms is not None:
            params["begin"] = requested.start_ms
        if requested and requested.end_ms is not None:
            params["end"] = requested.end_ms
        normalized: dict[int, list[Any]] = {}
        cursor_end = params.get("end")
        for _ in range(HISTORY_MAX_PAGES):
            request = {**params, **({"end": cursor_end} if cursor_end is not None else {})}
            payload = await self.transport.get(
                "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history",
                request,
            )
            if not isinstance(payload, dict) or str(payload.get("code")) != "0" or not isinstance(payload.get("data"), list):
                raise InvalidResponse("venue returned an invalid open-interest history")
            page = payload["data"]
            if any(not isinstance(item, list) or len(item) < 3 for item in page):
                raise InvalidResponse("venue returned an invalid open-interest history row")
            for item in page:
                timestamp = int(item[0])
                if requested and requested.start_ms is not None and timestamp < requested.start_ms:
                    continue
                if requested and requested.end_ms is not None and timestamp > requested.end_ms:
                    continue
                normalized[timestamp] = item
            if len(page) < 100:
                return [normalized[key] for key in sorted(normalized)]
            oldest = min(int(item[0]) for item in page)
            if requested and requested.start_ms is not None and oldest <= requested.start_ms:
                return [normalized[key] for key in sorted(normalized)]
            advanced = oldest - 1
            if cursor_end is not None and advanced >= cursor_end:
                raise PaginationError("open-interest history pagination did not advance")
            cursor_end = advanced
        raise PaginationError("open-interest history exceeded the bounded page limit")


