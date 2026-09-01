from __future__ import annotations

from ._common import (
    ContractDirection,
    DataUnavailable,
    HistoryRange,
    Instrument,
    InvalidResponse,
    NativeAdapter,
    NativeUnit,
    OpenInterestCapabilities,
    OpenInterestObservation,
    OpenInterestResult,
    ValuationMethod,
    contract_value_usd,
    number,
    proven_base_quantity,
)


class MexcAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "MEXC"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(
            True,
            False,
            required_metadata=("contract_direction", "contract_multiplier"),
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        payload = await self.transport.get(
            "https://contract.mexc.com/api/v1/contract/ticker"
        )
        if (
            not isinstance(payload, dict)
            or payload.get("success") is not True
            or payload.get("code") != 0
        ):
            raise InvalidResponse(
                "venue rejected the aggregate contract ticker request"
            )
        rows = payload.get("data")
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

        matches = [row for row in rows if row["symbol"] == instrument.symbol]
        if len(matches) != 1:
            raise DataUnavailable(
                "instrument does not resolve to exactly one aggregate contract ticker row"
            )
        row = matches[0]
        contracts = number(row.get("holdVol"))
        mark = number(row.get("fairPrice"))
        if mark <= 0:
            raise InvalidResponse("venue returned a non-positive fair price")
        timestamp = number(row.get("timestamp"))
        if timestamp < 0 or not timestamp.is_integer():
            raise InvalidResponse("venue returned an invalid source timestamp")

        value = contract_value_usd(instrument, contracts, mark)
        valuation = (
            ValuationMethod.MARK_PRICE
            if instrument.contract_direction is ContractDirection.LINEAR
            else ValuationMethod.CONTRACT_VALUE
        )
        return OpenInterestResult(
            OpenInterestObservation(
                int(timestamp),
                value,
                contracts,
                NativeUnit.CONTRACTS,
                mark,
                valuation,
                proven_base_quantity(instrument, contracts, NativeUnit.CONTRACTS),
            )
        )


