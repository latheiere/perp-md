from __future__ import annotations

from ._common import (
    HistoryRange,
    Instrument,
    InvalidInstrument,
    InvalidResponse,
    NativeAdapter,
    OpenInterestCapabilities,
    OpenInterestResult,
    _base_observation,
    _require_identity,
    number,
)


class GrvtAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "GRVT"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, False)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        if instrument.market_type not in ("perpetual", "future"):
            raise InvalidInstrument("provider open interest supports futures contracts only")
        payload = await self.transport.post(
            "https://market-data.grvt.io/full/v1/ticker",
            {"instrument": instrument.symbol},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            raise InvalidResponse("venue returned an invalid open-interest snapshot")
        row = payload["result"]
        _require_identity(row, "instrument", instrument.symbol)
        timestamp_ns = number(row.get("event_time"))
        if timestamp_ns < 0 or not timestamp_ns.is_integer():
            raise InvalidResponse("venue returned an invalid open-interest source timestamp")
        return OpenInterestResult(
            _base_observation(
                int(timestamp_ns) // 1_000_000,
                row.get("open_interest"),
                row.get("mark_price"),
            )
        )


