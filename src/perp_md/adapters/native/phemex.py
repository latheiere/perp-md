from __future__ import annotations

from ._common import (
    ContractDirection,
    HistoryRange,
    Instrument,
    InvalidResponse,
    NativeAdapter,
    OpenInterestCapabilities,
    OpenInterestResult,
    _base_observation,
    _inverse_contract_observation,
    _require_identity,
    number,
)


class PhemexAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "PHEMEX"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        required = (
            ("contract_multiplier",)
            if instrument.contract_direction is ContractDirection.INVERSE
            else ()
        )
        return OpenInterestCapabilities(True, False, required_metadata=required)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        inverse = instrument.contract_direction is ContractDirection.INVERSE
        payload = await self.transport.get(
            f"https://api.phemex.com/md/v{'1' if inverse else '2'}/ticker/24hr",
            {"symbol": instrument.symbol},
        )
        if (
            not isinstance(payload, dict)
            or payload.get("error") is not None
            or not isinstance(payload.get("result"), dict)
        ):
            raise InvalidResponse("venue returned an invalid open-interest snapshot")
        row = payload["result"]
        _require_identity(row, "symbol", instrument.symbol)
        timestamp_ns = number(row.get("timestamp"))
        if timestamp_ns < 0 or not timestamp_ns.is_integer():
            raise InvalidResponse("venue returned an invalid open-interest source timestamp")
        timestamp = int(timestamp_ns) // 1_000_000
        if inverse:
            return OpenInterestResult(
                _inverse_contract_observation(
                    instrument, timestamp, row.get("openInterest")
                )
            )
        return OpenInterestResult(
            _base_observation(timestamp, row.get("openInterestRv"), row.get("markPriceRp"))
        )


