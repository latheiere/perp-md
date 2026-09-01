from __future__ import annotations

from ._common import (
    ContractDirection,
    DataUnavailable,
    HistoryRange,
    Instrument,
    InvalidInstrument,
    InvalidResponse,
    NativeAdapter,
    OpenInterestCapabilities,
    OpenInterestObservation,
    OpenInterestResult,
    ValuationMethod,
    _integer_ms,
    _require_identity,
    number,
)


class XtAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "XT"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(
            True, False, required_metadata=("contract_direction",)
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        if instrument.contract_direction is ContractDirection.LINEAR:
            host = "https://fapi.xt.com"
        elif instrument.contract_direction is ContractDirection.INVERSE:
            host = "https://dapi.xt.com"
        else:
            raise InvalidInstrument(
                "contract_direction is required for provider product routing"
            )
        payload = await self.transport.get(
            f"{host}/future/market/v1/public/contract/open-interest",
            {"symbol": instrument.symbol},
        )
        if (
            not isinstance(payload, dict)
            or str(payload.get("returnCode")) != "0"
            or not isinstance(payload.get("result"), dict)
        ):
            raise InvalidResponse("provider returned an invalid open-interest snapshot")
        row = payload["result"]
        _require_identity(row, "symbol", instrument.symbol)
        if row.get("openInterestUsd") in (None, "") or row.get("time") is None:
            raise DataUnavailable(
                "provider omitted open-interest notional or source time"
            )
        return OpenInterestResult(
            OpenInterestObservation(
                _integer_ms(row["time"], "open-interest"),
                number(row["openInterestUsd"]),
                valuation=ValuationMethod.VENUE_REPORTED,
            )
        )


