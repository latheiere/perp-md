from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cdm import DerivationKind, FundingIntervalKind, FundingRateKind, TemporalMode

from perp_md import (
    ContractDirection,
    FundingClient,
    HistoryRange,
    Instrument,
    InvalidInstrument,
)
from perp_md.adapters.ccxt_funding import CcxtFundingAdapter
from perp_md.adapters.funding import (
    BinanceFundingAdapter,
    BybitFundingAdapter,
    GateFundingAdapter,
    HyperliquidFundingAdapter,
    KrakenFundingAdapter,
    OkxFundingAdapter,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "native_funding.json").read_text()
)


class StubTransport:
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    async def get(self, url, params=None):
        self.requests.append(("GET", url, params))
        return await self.handler("GET", url, params)

    async def post(self, url, payload):
        self.requests.append(("POST", url, payload))
        return await self.handler("POST", url, payload)

    async def close(self):
        return None


def instrument(venue: str, **values: Any) -> Instrument:
    defaults = {
        "venue": venue,
        "symbol": "BASEQUOTE",
        "base_currency": "BASE",
        "quote_currency": "QUOTE",
        "settlement_currency": "QUOTE",
        "contract_direction": ContractDirection.LINEAR,
        "contract_multiplier": 1,
    }
    return Instrument(**{**defaults, **values})


def test_relative_current_and_settled_history_preserve_temporal_semantics():
    async def handler(method, url, params):
        if url.endswith("premiumIndex"):
            return FIXTURE["binance"]["current"]
        return FIXTURE["binance"]["history"]

    result = asyncio.run(
        BinanceFundingAdapter(StubTransport(handler)).fetch(
            instrument("BINANCE"), None, include_history=True
        )
    )

    assert result.current.kind is FundingRateKind.INDICATIVE
    assert result.current.sample.observed_at is not None
    assert result.current.sample.effective_at is None
    assert result.history[0].kind is FundingRateKind.SETTLED
    assert result.history[0].sample.effective_at is not None
    assert result.history[0].sample.observed_at is None
    assert result.current.interval.kind is FundingIntervalKind.UNSPECIFIED
    assert result.history[0].interval.kind is FundingIntervalKind.UNSPECIFIED


def test_regular_history_spacing_does_not_invent_an_interval():
    history = [
        {"fundingTime": 1_000, "fundingRate": "0.1"},
        {"fundingTime": 2_000, "fundingRate": "0.2"},
    ]

    async def handler(method, url, params):
        if url.endswith("premiumIndex"):
            return {"lastFundingRate": "0.3", "time": 3_000}
        return history

    result = asyncio.run(
        BinanceFundingAdapter(StubTransport(handler)).fetch(
            instrument("BINANCE"), None, include_history=True
        )
    )

    assert all(
        point.interval.kind is FundingIntervalKind.UNSPECIFIED
        for point in (result.current, *result.history)
    )


def test_malformed_history_preserves_current_funding():
    async def handler(method, url, params):
        if url.endswith("premiumIndex"):
            return FIXTURE["binance"]["current"]
        return FIXTURE["binance"]["malformed_history"]

    result = asyncio.run(
        BinanceFundingAdapter(StubTransport(handler)).fetch(
            instrument("BINANCE"), None, include_history=True
        )
    )

    assert result.current.rate == pytest.approx(0.0001)
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"


def test_provider_reported_interval_is_not_applied_to_history_retroactively():
    async def handler(method, url, params):
        if url.endswith("tickers"):
            return FIXTURE["bybit"]["current"]
        return FIXTURE["bybit"]["history"]

    result = asyncio.run(
        BybitFundingAdapter(StubTransport(handler)).fetch(
            instrument("BYBIT"), None, include_history=True
        )
    )

    assert result.current.interval.kind is FundingIntervalKind.EXPLICIT_DURATION
    assert result.current.interval.duration_seconds == 28_800
    assert result.history[0].interval.kind is FundingIntervalKind.UNSPECIFIED


def test_rejected_history_is_structured_partial_success():
    async def handler(method, url, params):
        if url.endswith("tickers"):
            return FIXTURE["bybit"]["current"]
        return FIXTURE["bybit"]["rejected_history"]

    result = asyncio.run(
        BybitFundingAdapter(StubTransport(handler)).fetch(
            instrument("BYBIT"), None, include_history=True
        )
    )

    assert result.current.rate == pytest.approx(0.0001)
    assert result.history == ()
    assert result.history_issue is not None


def test_retrieval_timestamp_and_provider_interval_are_explicit():
    async def handler(method, url, params):
        if url.endswith("funding_rate"):
            return FIXTURE["gate"]["history"]
        return FIXTURE["gate"]["current"]

    result = asyncio.run(
        GateFundingAdapter(StubTransport(handler), lambda: 1_700_000_100).fetch(
            instrument("GATE"), None, include_history=True
        )
    )

    assert result.current.sample.observed_at is None
    assert (
        int(result.current.provider_evidence.retrieved_at.timestamp()) == 1_700_000_100
    )
    assert result.current.interval.kind is FundingIntervalKind.EXPLICIT_DURATION
    assert result.current.interval.duration_seconds == 28_800
    assert result.history[0].rate == pytest.approx(0.00009)


def test_endpoint_identity_requires_explicit_settlement_currency():
    adapter = GateFundingAdapter(StubTransport(None))
    with pytest.raises(InvalidInstrument, match="settlement_currency"):
        asyncio.run(
            adapter.fetch(
                instrument("GATE", settlement_currency=None),
                None,
                include_history=False,
            )
        )


def test_history_only_provider_labels_latest_settlement_as_current():
    async def handler(method, url, params):
        return FIXTURE["okx"]

    result = asyncio.run(
        OkxFundingAdapter(StubTransport(handler)).fetch(
            instrument("OKX"), None, include_history=True
        )
    )

    assert result.current.kind is FundingRateKind.SETTLED
    assert result.current.sample.lineage.output.temporal_mode is TemporalMode.SETTLED
    assert result.current.timestamp_ms == 1_700_000_000_000
    assert result.history == tuple(
        sorted(result.history, key=lambda point: point.timestamp_ms)
    )


def test_protocol_interval_is_evidence_not_timestamp_inference():
    async def handler(method, url, params):
        return FIXTURE["hyperliquid"]

    result = asyncio.run(
        HyperliquidFundingAdapter(StubTransport(handler), lambda: 1_700_000_000).fetch(
            instrument("HYPERLIQUID"), None, include_history=True
        )
    )

    assert result.current.interval.kind is FundingIntervalKind.PROTOCOL_SCHEDULE
    assert result.current.interval.duration_seconds is None
    assert result.current.sample.lineage.output.temporal_mode is TemporalMode.SETTLED


def test_optional_abstraction_does_not_invent_time_or_numeric_interval_units():
    observation = CcxtFundingAdapter._observation(
        None,
        "0.0001",
        FundingRateKind.INDICATIVE,
        TemporalMode.CURRENT,
        CcxtFundingAdapter._reported_interval({"interval": 28_800_000}),
        retrieved_at_ms=1_700_000_000_000,
    )

    assert observation.sample.observed_at is None
    assert observation.sample.rate == Decimal("0.0001")
    assert observation.provider_evidence.retrieved_at is not None
    assert observation.interval.kind is FundingIntervalKind.UNSPECIFIED


def test_history_only_optional_runtime_keeps_settled_current_semantics():
    class Exchange:
        has = {
            "fetchFundingRate": False,
            "fetchFundingRateHistory": True,
        }

        async def fetch_funding_rate_history(self, symbol, *, since, limit):
            return [
                {
                    "timestamp": 1_700_000_000_000,
                    "fundingRate": "0.1234567890123456789",
                }
            ]

    class Adapter(CcxtFundingAdapter):
        async def _market(self, subject):
            return Exchange(), "EXACT-ENDPOINT-ID"

    result = asyncio.run(
        Adapter().fetch(
            instrument("DERIBIT"),
            None,
            include_history=False,
        )
    )

    assert result.current.kind is FundingRateKind.SETTLED
    assert result.current.sample.rate == Decimal("0.1234567890123456789")
    assert result.current.sample.lineage.output.temporal_mode is TemporalMode.SETTLED
    assert result.history == ()


@pytest.mark.parametrize(
    ("symbol", "direction", "expected_rate", "method_id"),
    [
        (
            "LINEAR-PERP",
            ContractDirection.LINEAR,
            0.0001,
            "perp_md.funding.absolute_to_relative.linear.v1",
        ),
        (
            "INVERSE-PERP",
            ContractDirection.INVERSE,
            -0.125,
            "perp_md.funding.absolute_to_relative.inverse.v1",
        ),
    ],
)
def test_absolute_current_funding_preserves_normalization_evidence(
    symbol, direction, expected_rate, method_id
):
    async def handler(method, url, params):
        return FIXTURE["kraken"]["current"]

    result = asyncio.run(
        KrakenFundingAdapter(StubTransport(handler)).fetch(
            instrument("KRAKEN", symbol=symbol, contract_direction=direction),
            None,
            include_history=False,
        )
    )

    assert result.current.rate == pytest.approx(expected_rate)
    final_step = result.current.sample.lineage.steps[-1]
    assert final_step.kind is DerivationKind.PROVIDER_FORMULA
    assert final_step.method_id == method_id
    assert result.current.provider_evidence.mark_price is not None


def test_full_retained_history_requires_start_without_losing_current():
    async def handler(method, url, params):
        return FIXTURE["kraken"]["current"]

    result = asyncio.run(
        KrakenFundingAdapter(StubTransport(handler)).fetch(
            instrument("KRAKEN", symbol="LINEAR-PERP"),
            None,
            include_history=True,
        )
    )

    assert result.current.rate == pytest.approx(0.0001)
    assert result.history_issue is not None
    assert result.history_issue.code == "history_range_required"


def test_sparse_full_retained_history_is_sorted_without_interval_inference():
    async def handler(method, url, params):
        if url.endswith("tickers"):
            return FIXTURE["kraken"]["current"]
        return FIXTURE["kraken"]["history"]

    result = asyncio.run(
        KrakenFundingAdapter(StubTransport(handler)).fetch(
            instrument("KRAKEN", symbol="LINEAR-PERP"),
            HistoryRange(1_699_970_000_000),
            include_history=True,
        )
    )

    assert [point.rate for point in result.history] == [0.00008, 0.00007, 0.00009]
    assert all(
        point.interval.kind is FundingIntervalKind.PROTOCOL_SCHEDULE
        for point in result.history
    )


def test_funding_client_is_independent_from_open_interest_client():
    class Adapter:
        closed = False

        def supports(self, subject):
            return True

        def capabilities(self, subject):
            return BybitFundingAdapter(StubTransport(None)).capabilities(subject)

        async def fetch(self, subject, history, *, include_history):
            return await BybitFundingAdapter(StubTransport(handler)).fetch(
                subject, history, include_history=include_history
            )

        async def close(self):
            self.closed = True

    async def handler(method, url, params):
        return (
            FIXTURE["bybit"]["current"]
            if url.endswith("tickers")
            else FIXTURE["bybit"]["history"]
        )

    adapter = Adapter()
    client = FundingClient(
        adapters={"BYBIT": adapter}, transport=StubTransport(handler)
    )
    result = asyncio.run(client.fetch(instrument("BYBIT"), include_history=False))
    asyncio.run(client.close())

    assert result.current.rate == pytest.approx(0.0001)
    assert adapter.closed is True
