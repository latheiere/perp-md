from __future__ import annotations

from ._common import (
    DataUnavailable,
    HistoryRange,
    Instrument,
    InvalidInstrument,
    InvalidResponse,
    NativeAdapter,
    ObservationTimeKind,
    OpenInterestCapabilities,
    OpenInterestResult,
    _base_observation,
)


class LighterAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "LIGHTER"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, False)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        payload = await self.transport.get(
            "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails",
            {"market_id": instrument.symbol},
        )
        if (
            not isinstance(payload, dict)
            or payload.get("code") != 200
            or not isinstance(payload.get("order_book_details"), list)
        ):
            raise InvalidResponse("venue returned an invalid open-interest snapshot")
        rows = payload["order_book_details"]
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise DataUnavailable("instrument does not resolve to one open-interest row")
        row = rows[0]
        if str(row.get("market_id")) != instrument.symbol:
            raise InvalidResponse("venue returned a mismatched instrument identity")
        if row.get("market_type") != "perp":
            raise InvalidInstrument("provider market identity is not a perpetual contract")
        return OpenInterestResult(
            _base_observation(
                int(self.clock() * 1_000),
                row.get("open_interest"),
                row.get("mark_price"),
                timestamp_kind=ObservationTimeKind.RETRIEVED,
            )
        )


