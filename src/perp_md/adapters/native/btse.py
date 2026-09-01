from __future__ import annotations

from ._common import (
    DataUnavailable,
    HistoryRange,
    Instrument,
    NativeAdapter,
    OpenInterestCapabilities,
    OpenInterestResult,
    _btse_rows,
    _contract_observation,
    _require_identity,
    asyncio,
)


class BtseAdapter(NativeAdapter):
    BASE_URL = "https://api.btse.com/public-api/market/v1/ticker"

    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "BTSE"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(
            True, False, required_metadata=("contract_multiplier",)
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        ticker_payload, index_payload = await asyncio.gather(
            self.transport.get(
                f"{self.BASE_URL}/24hr", {"symbol": instrument.symbol}
            ),
            self.transport.get(
                f"{self.BASE_URL}/indices", {"symbol": instrument.symbol}
            ),
        )
        ticker_rows, timestamp = _btse_rows(ticker_payload, "open-interest snapshot")
        index_rows, _ = _btse_rows(index_payload, "mark-price snapshot")
        if len(ticker_rows) != 1 or len(index_rows) != 1:
            raise DataUnavailable("instrument does not resolve to one current market row")
        ticker, index = ticker_rows[0], index_rows[0]
        _require_identity(ticker, "symbol", instrument.symbol)
        _require_identity(index, "symbol", instrument.symbol)
        return OpenInterestResult(
            _contract_observation(
                instrument,
                timestamp,
                ticker.get("openInterest"),
                index.get("markPrice"),
            )
        )


