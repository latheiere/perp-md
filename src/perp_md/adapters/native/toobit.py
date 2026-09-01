from __future__ import annotations

from ._common import (
    DataUnavailable,
    HistoryRange,
    Instrument,
    InvalidResponse,
    NativeAdapter,
    ObservationTimeKind,
    OpenInterestCapabilities,
    OpenInterestResult,
    _base_observation,
    _require_identity,
    asyncio,
)


class ToobitAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "TOOBIT"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, False)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        oi_payload, mark = await asyncio.gather(
            self.transport.get(
                "https://api.toobit.com/quote/v1/openInterest",
                {"symbol": instrument.symbol},
            ),
            self.transport.get(
                "https://api.toobit.com/quote/v1/markPrice",
                {"symbol": instrument.symbol},
            ),
        )
        if not isinstance(oi_payload, dict) or not isinstance(
            oi_payload.get("openInterestList"), list
        ):
            raise InvalidResponse("venue returned an invalid open-interest snapshot")
        rows = oi_payload["openInterestList"]
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise DataUnavailable("instrument does not resolve to one open-interest row")
        if not isinstance(mark, dict):
            raise InvalidResponse("venue returned an invalid mark-price snapshot")
        row = rows[0]
        _require_identity(row, "symbol", instrument.symbol)
        _require_identity(mark, "symbolId", instrument.symbol)
        return OpenInterestResult(
            _base_observation(
                int(self.clock() * 1_000),
                row.get("size"),
                mark.get("price"),
                timestamp_kind=ObservationTimeKind.RETRIEVED,
            )
        )


