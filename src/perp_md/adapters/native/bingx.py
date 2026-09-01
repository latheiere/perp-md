from __future__ import annotations

from ._common import (
    Any,
    Awaitable,
    BINGX_API_URL,
    Callable,
    ContractDirection,
    Decimal,
    HistoryRange,
    Instrument,
    InvalidInstrument,
    InvalidResponse,
    NativeAdapter,
    NativeUnit,
    OpenInterestCapabilities,
    OpenInterestMeasure,
    OpenInterestObservation,
    OpenInterestResult,
    OpenInterestValueV1,
    ValuationMethod,
    _bingx_row,
    _integer_ms,
    asyncio,
    dataclass,
    field,
    number,
    time,
)
from perp_md.pacing import RequestPacer


@dataclass
class BingxAdapter(NativeAdapter):
    request_interval_seconds: float = 1.0
    request_clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    _request_pacer: RequestPacer = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._request_pacer = RequestPacer(
            self.request_interval_seconds,
            clock=self.request_clock,
            sleep=self.sleep,
        )

    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "BINGX"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        self._direction(instrument)
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
        direction = self._direction(instrument)
        linear = direction is ContractDirection.LINEAR
        family = "swap/v2/quote" if linear else "cswap/v1/market"
        params: dict[str, Any] = {"symbol": instrument.symbol}
        if not linear:
            params["timestamp"] = int(self.clock() * 1_000)
        oi_payload, premium_payload = await self._request_pacer.request(
            lambda: asyncio.gather(
                self.transport.get(
                    f"{BINGX_API_URL}/{family}/openInterest", params
                ),
                self.transport.get(
                    f"{BINGX_API_URL}/{family}/premiumIndex", params
                ),
            )
        )
        category = "linear" if linear else "inverse"
        oi = _bingx_row(
            oi_payload, instrument.symbol, f"{category} open-interest snapshot"
        )
        premium = _bingx_row(
            premium_payload, instrument.symbol, f"{category} mark-price snapshot"
        )
        raw = number(oi.get("openInterest"))
        mark = number(premium.get("markPrice"))
        if raw < 0 or mark <= 0:
            raise InvalidResponse(
                "venue returned invalid open-interest normalization inputs"
            )
        timestamp = _integer_ms(
            oi.get("time") if linear else oi_payload.get("timestamp"),
            "open-interest",
        )
        current = OpenInterestObservation(
            timestamp,
            raw if linear else raw * mark,
            raw,
            NativeUnit.QUOTE if linear else NativeUnit.BASE,
            mark,
            ValuationMethod.VENUE_REPORTED if linear else ValuationMethod.MARK_PRICE,
            OpenInterestValueV1(
                Decimal(str(raw)) / Decimal(str(mark)) if linear else Decimal(str(raw)),
                OpenInterestMeasure.BASE_QUANTITY,
            ),
        )
        return OpenInterestResult(current)

    @staticmethod
    def _direction(instrument: Instrument) -> ContractDirection:
        if instrument.contract_direction in (
            ContractDirection.LINEAR,
            ContractDirection.INVERSE,
        ):
            return instrument.contract_direction
        raise InvalidInstrument(
            "contract_direction is required for provider product routing"
        )
