from __future__ import annotations

from ._common import (
    Any,
    Decimal,
    GATE_HISTORY_LIMIT,
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
    OpenInterestMeasure,
    OpenInterestObservation,
    OpenInterestResult,
    OpenInterestValueV1,
    PaginationError,
    ValuationMethod,
    number,
)


class GateAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "GATE"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(
            True,
            instrument.market_type != "future",
            300 if instrument.market_type != "future" else None,
            required_metadata=("settlement_currency",),
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        if not instrument.settlement_currency:
            raise InvalidInstrument(
                "settlement_currency is required for the provider endpoint identity"
            )
        settle = instrument.settlement_currency.lower()
        route = "delivery" if instrument.market_type == "future" else "futures"
        details = await self.transport.get(
            f"https://api.gateio.ws/api/v4/{route}/{settle}/contracts/{instrument.symbol}"
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
        base_quantity = None
        if str(details.get("type", "")).lower() != "inverse":
            source_multiplier = number(details.get("quanto_multiplier"))
            base_quantity = OpenInterestValueV1(
                Decimal(str(native * source_multiplier)),
                OpenInterestMeasure.BASE_QUANTITY,
            )
        current = OpenInterestObservation(
            int(self.clock() * 1000),
            value,
            native,
            NativeUnit.CONTRACTS,
            mark,
            valuation,
            base_quantity,
            ObservationTimeKind.RETRIEVED,
        )
        if not include_history:
            return OpenInterestResult(current)
        if instrument.market_type == "future":
            return OpenInterestResult(current)
        try:
            payload = await self._history(settle, instrument.symbol, history)
            observations: list[OpenInterestObservation] = []
            for item in payload:
                native = number(item["open_interest"])
                notional = number(item["open_interest_usd"])
                mark = number(item["mark_price"])
                if mark <= 0:
                    raise InvalidResponse(
                        "venue returned a non-positive historical mark price"
                    )
                base = OpenInterestValueV1(
                    Decimal(str(notional)) / Decimal(str(mark)),
                    OpenInterestMeasure.BASE_QUANTITY,
                )
                observations.append(
                    OpenInterestObservation(
                        int(item["time"]) * 1000,
                        notional,
                        native,
                        NativeUnit.CONTRACTS,
                        mark,
                        ValuationMethod.VENUE_REPORTED,
                        base,
                    )
                )
            return OpenInterestResult(current, tuple(observations))
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
        current_bucket = (
            int(self.clock() * 1000) // HISTORY_BUCKET_MS * HISTORY_BUCKET_MS
        )
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
            # Statistics pages can omit native buckets and therefore contain
            # fewer rows than the requested limit before the requested range
            # has been traversed. Only the source timestamp proves completion.
            if newest * 1000 >= end:
                return [rows[key] for key in sorted(rows)]
            advanced = newest + 1
            if advanced <= next_from:
                raise PaginationError(
                    "open-interest history pagination did not advance"
                )
            next_from = advanced
        raise PaginationError("open-interest history exceeded the bounded page limit")


