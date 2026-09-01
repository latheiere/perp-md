from __future__ import annotations

from ._common import (
    Any,
    ContractDirection,
    DataUnavailable,
    HTX_HISTORY_LIMIT,
    HistoryRange,
    Instrument,
    InvalidInstrument,
    InvalidResponse,
    NativeAdapter,
    OpenInterestCapabilities,
    OpenInterestResult,
    _htx_history_rows,
    _htx_observation,
    _htx_rows,
)


class HtxAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "HTX"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        required = ("contract_direction",)
        if instrument.contract_direction is ContractDirection.INVERSE:
            required += ("contract_multiplier",)
        return OpenInterestCapabilities(True, True, 300, required_metadata=required)

    @staticmethod
    def _prefix(instrument: Instrument) -> str:
        if instrument.market_type == "future" and instrument.contract_direction is ContractDirection.INVERSE:
            return "api/v1"
        if instrument.contract_direction is ContractDirection.LINEAR:
            return "linear-swap-api/v1"
        if instrument.contract_direction is ContractDirection.INVERSE:
            return "swap-api/v1"
        raise InvalidInstrument("contract_direction is required for provider product routing")

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        prefix = self._prefix(instrument)
        future = instrument.market_type == "future"
        current_endpoint = (
            "contract_open_interest"
            if future and instrument.contract_direction is ContractDirection.INVERSE
            else "swap_open_interest"
        )
        current_params: dict[str, Any] = {"contract_code": instrument.symbol}
        if future and instrument.contract_direction is ContractDirection.LINEAR:
            current_params["business_type"] = "futures"
        payload = await self.transport.get(
            f"https://api.hbdm.com/{prefix}/{current_endpoint}",
            current_params,
        )
        rows, source_time = _htx_rows(payload, "open-interest snapshot")
        if len(rows) != 1:
            raise DataUnavailable("instrument does not resolve to one open-interest row")
        current = _htx_observation(instrument, rows[0], source_time)
        if not include_history:
            return OpenInterestResult(current)
        if future and instrument.contract_direction is ContractDirection.INVERSE:
            current_row = rows[0]
            if current_row.get("symbol") is None or current_row.get("contract_type") is None:
                return OpenInterestResult(
                    current,
                    history_issue=self._issue(
                        InvalidResponse(
                            "venue omitted the dated history identity from current open interest"
                        )
                    ),
                )
            try:
                hist = await self.transport.get(
                    "https://api.hbdm.com/api/v1/contract_his_open_interest",
                    {
                        "symbol": current_row["symbol"],
                        "contract_type": current_row["contract_type"],
                        "period": "60min",
                        "size": HTX_HISTORY_LIMIT,
                        "amount_type": 1,
                    },
                )
                history_rows = _htx_history_rows(hist, history)
                return OpenInterestResult(
                    current,
                    tuple(
                        _htx_observation(instrument, row, int(row["ts"]))
                        for row in history_rows
                    ),
                )
            except Exception as exc:
                return OpenInterestResult(current, history_issue=self._issue(exc))
        try:
            history_params: dict[str, Any] = {
                "contract_code": instrument.symbol,
                "period": "5min",
                "size": HTX_HISTORY_LIMIT,
                "amount_type": 1,
            }
            if future:
                history_params["business_type"] = "futures"
            hist = await self.transport.get(
                f"https://api.hbdm.com/{prefix}/swap_his_open_interest",
                history_params,
            )
            history_rows = _htx_history_rows(hist, history)
            return OpenInterestResult(
                current,
                tuple(_htx_observation(instrument, row, int(row["ts"])) for row in history_rows),
            )
        except Exception as exc:
            return OpenInterestResult(current, history_issue=self._issue(exc))


