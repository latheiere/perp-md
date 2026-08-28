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
    InvalidResponse,
)
from perp_md.adapters import funding as funding_module
from perp_md.adapters.ccxt_funding import CcxtFundingAdapter
from perp_md.adapters.funding import (
    BinanceFundingAdapter,
    BybitFundingAdapter,
    DeepcoinFundingAdapter,
    GateFundingAdapter,
    HtxFundingAdapter,
    HyperliquidFundingAdapter,
    BtseFundingAdapter,
    GrvtFundingAdapter,
    KrakenFundingAdapter,
    KucoinFundingAdapter,
    LighterFundingAdapter,
    OkxFundingAdapter,
    ToobitFundingAdapter,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "native_funding.json").read_text()
)
ADDED_VENUES = json.loads(
    (Path(__file__).parent / "fixtures" / "native_added_venues.json").read_text()
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


@pytest.mark.parametrize(
    ("adapter", "venue", "symbol", "fixture_key"),
    [
        (ToobitFundingAdapter, "TOOBIT", "BASE-SWAP-QUOTE", "toobit"),
        (LighterFundingAdapter, "LIGHTER", "7", "lighter"),
        (GrvtFundingAdapter, "GRVT", "BASE_QUOTE_Perp", "grvt"),
        (BtseFundingAdapter, "BTSE", "BASE-PERP-QUOTE", "btse"),
    ],
)
def test_ranked_native_funding_preserves_current_and_settled_protocol_semantics(
    adapter, venue, symbol, fixture_key
):
    fixture = ADDED_VENUES[fixture_key]

    async def handler(method, url, params):
        return fixture["funding_history"] if "history" in url or url.endswith("/funding") or url.endswith("/fundings") else fixture["funding_current" if "funding_current" in fixture else "current"]

    result = asyncio.run(
        adapter(StubTransport(handler), lambda: 1_700_000_100).fetch(
            instrument(venue, symbol=symbol), None, include_history=True
        )
    )

    assert result.current.rate == pytest.approx(0.0001)
    assert result.history[0].rate == pytest.approx(0.00008)
    assert result.history[0].kind is FundingRateKind.SETTLED


@pytest.mark.parametrize(
    ("adapter", "venue", "symbol", "fixture_key"),
    [
        (ToobitFundingAdapter, "TOOBIT", "BASE-SWAP-QUOTE", "toobit"),
        (LighterFundingAdapter, "LIGHTER", "7", "lighter"),
        (GrvtFundingAdapter, "GRVT", "BASE_QUOTE_Perp", "grvt"),
        (BtseFundingAdapter, "BTSE", "BASE-PERP-QUOTE", "btse"),
    ],
)
def test_ranked_native_funding_keeps_valid_current_when_history_is_malformed(
    adapter, venue, symbol, fixture_key
):
    fixture = ADDED_VENUES[fixture_key]

    async def handler(method, url, params):
        if "history" in url or url.endswith("/funding") or url.endswith("/fundings"):
            return {"unexpected": []}
        return fixture["funding_current" if "funding_current" in fixture else "current"]

    result = asyncio.run(
        adapter(StubTransport(handler), lambda: 1_700_000_100).fetch(
            instrument(venue, symbol=symbol), None, include_history=True
        )
    )

    assert result.current.rate == pytest.approx(0.0001)
    assert result.history == ()
    assert result.history_issue is not None


def test_identifier_cursor_history_uses_the_older_page_direction(monkeypatch):
    fixture = ADDED_VENUES["toobit"]
    pages = fixture["funding_history_pages"]
    monkeypatch.setattr(funding_module, "TOOBIT_FUNDING_HISTORY_LIMIT", 2)

    async def handler(method, url, params):
        if url.endswith("fundingRate"):
            return fixture["funding_current"]
        return pages[1] if params.get("fromId") == 3 else pages[0]

    transport = StubTransport(handler)
    result = asyncio.run(
        ToobitFundingAdapter(transport, lambda: 1_700_000_100).fetch(
            instrument("TOOBIT", symbol="BASE-SWAP-QUOTE"),
            HistoryRange(1_699_940_000_000),
            include_history=True,
        )
    )

    history_requests = [request for request in transport.requests if "history" in request[1]]
    assert history_requests[1][2] == {
        "symbol": "BASE-SWAP-QUOTE",
        "limit": 2,
        "fromId": 3,
    }
    assert [point.timestamp_ms for point in result.history] == [
        1_699_942_400_000,
        1_699_971_200_000,
        1_700_000_000_000,
    ]
    assert result.history_issue is None


def test_relative_current_and_settled_history_preserve_temporal_semantics():
    async def handler(method, url, params):
        if url.endswith("premiumIndex"):
            return FIXTURE["binance"]["current"]
        if url.endswith("fundingInfo"):
            return FIXTURE["binance"]["funding_info"]
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
    assert result.current.interval.kind is FundingIntervalKind.EXPLICIT_DURATION
    assert result.current.interval.duration_seconds == 14_400
    assert result.history[0].interval.kind is FundingIntervalKind.UNSPECIFIED


def test_settlement_spacing_is_preserved_as_observed_window_evidence():
    history = [
        {"fundingTime": 1_000, "fundingRate": "0.1"},
        {"fundingTime": 2_000, "fundingRate": "0.2"},
    ]

    async def handler(method, url, params):
        if url.endswith("premiumIndex"):
            return {"lastFundingRate": "0.3", "time": 3_000}
        if url.endswith("fundingInfo"):
            return []
        return history

    result = asyncio.run(
        BinanceFundingAdapter(StubTransport(handler)).fetch(
            instrument("BINANCE"), None, include_history=True
        )
    )

    assert result.current.interval.kind is FundingIntervalKind.UNSPECIFIED
    assert result.history[0].interval.kind is FundingIntervalKind.UNSPECIFIED
    assert result.history[1].interval.kind is FundingIntervalKind.OBSERVED_WINDOW
    assert result.history[1].interval.duration_seconds == 1
    assert result.history[1].interval.window_start is not None


def test_current_settlement_boundaries_supply_cycle_duration_when_adjustment_is_absent():
    async def handler(method, url, params):
        if url.endswith("premiumIndex"):
            return {
                "lastFundingRate": "0.0001",
                "time": 28_900_000,
                "nextFundingTime": 57_600_000,
            }
        if url.endswith("fundingInfo"):
            return []
        return [{"fundingTime": 28_800_000, "fundingRate": "0.00009"}]

    result = asyncio.run(
        BinanceFundingAdapter(StubTransport(handler)).fetch(
            instrument("BINANCE"), None, include_history=False
        )
    )

    assert result.current.interval.kind is FundingIntervalKind.EXPLICIT_DURATION
    assert result.current.interval.duration_seconds == 28_800


def test_bounded_boundary_jitter_preserves_current_cycle_duration():
    fixture = FIXTURE["boundary_windows"]["current_with_bounded_jitter"]

    async def handler(method, url, params):
        if url.endswith("premiumIndex"):
            return fixture["snapshot"]
        if url.endswith("fundingInfo"):
            return []
        return fixture["latest_history"]

    result = asyncio.run(
        BinanceFundingAdapter(StubTransport(handler)).fetch(
            instrument("BINANCE"), None, include_history=False
        )
    )

    assert result.current.interval.kind is FundingIntervalKind.EXPLICIT_DURATION
    assert result.current.interval.duration_seconds == 14_400


def test_bounded_boundary_jitter_preserves_observed_history_duration():
    fixture = FIXTURE["boundary_windows"]["history_with_bounded_jitter"]

    async def handler(method, url, params):
        if url.endswith("premiumIndex"):
            return FIXTURE["binance"]["current"]
        if url.endswith("fundingInfo"):
            return []
        return fixture

    result = asyncio.run(
        BinanceFundingAdapter(StubTransport(handler)).fetch(
            instrument("BINANCE"), None, include_history=True
        )
    )

    assert result.history[1].interval.kind is FundingIntervalKind.OBSERVED_WINDOW
    assert result.history[1].interval.duration_seconds == 14_400
    assert result.history[1].timestamp_ms == fixture[1]["fundingTime"]
    assert result.history[1].interval.window_start is not None
    assert (
        int(result.history[1].interval.window_start.timestamp() * 1_000)
        == fixture[0]["fundingTime"]
    )


def test_current_cycle_boundary_jitter_outside_bound_remains_unspecified():
    fixture = FIXTURE["boundary_windows"]["current_outside_tolerance"]

    async def handler(method, url, params):
        if url.endswith("premiumIndex"):
            return fixture["snapshot"]
        if url.endswith("fundingInfo"):
            return []
        return fixture["latest_history"]

    result = asyncio.run(
        BinanceFundingAdapter(StubTransport(handler)).fetch(
            instrument("BINANCE"), None, include_history=False
        )
    )

    assert result.current.interval.kind is FundingIntervalKind.UNSPECIFIED


def test_ambiguous_history_window_is_rejected_as_partial_history():
    fixture = FIXTURE["boundary_windows"]["ambiguous_history"]

    async def handler(method, url, params):
        if url.endswith("premiumIndex"):
            return FIXTURE["binance"]["current"]
        if url.endswith("fundingInfo"):
            return []
        return fixture

    result = asyncio.run(
        BinanceFundingAdapter(StubTransport(handler)).fetch(
            instrument("BINANCE"), None, include_history=True
        )
    )

    assert result.history == ()
    assert result.history_issue is not None
    assert "unambiguous" in result.history_issue.message


def test_unavailable_interval_metadata_preserves_valid_current_snapshot():
    async def handler(method, url, params):
        if url.endswith("premiumIndex"):
            return FIXTURE["binance"]["current"]
        raise RuntimeError("interval endpoint unavailable")

    result = asyncio.run(
        BinanceFundingAdapter(StubTransport(handler)).fetch(
            instrument("BINANCE"), None, include_history=False
        )
    )

    assert result.current.rate == pytest.approx(0.0001)
    assert result.current.interval.kind is FundingIntervalKind.UNSPECIFIED


def test_malformed_history_preserves_current_funding():
    async def handler(method, url, params):
        if url.endswith("premiumIndex"):
            return FIXTURE["binance"]["current"]
        if url.endswith("fundingInfo"):
            return FIXTURE["binance"]["funding_info"]
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


def test_current_and_historical_provider_boundaries_preserve_distinct_evidence():
    async def handler(method, url, params):
        if url.endswith("funding-rate-history"):
            return FIXTURE["okx"]["history"]
        return FIXTURE["okx"]["current"]

    transport = StubTransport(handler)
    result = asyncio.run(
        OkxFundingAdapter(transport).fetch(
            instrument("OKX"), None, include_history=True
        )
    )

    assert result.current.kind is FundingRateKind.INDICATIVE
    assert result.current.sample.lineage.output.temporal_mode is TemporalMode.CURRENT
    assert result.current.timestamp_ms == 1_699_999_900_000
    assert result.current.interval.kind is FundingIntervalKind.EXPLICIT_DURATION
    assert result.current.interval.duration_seconds == 14_400
    assert result.history[0].interval.kind is FundingIntervalKind.UNSPECIFIED
    assert result.history[1].interval.kind is FundingIntervalKind.OBSERVED_WINDOW
    assert result.history[1].interval.duration_seconds == 28_800
    assert result.history == tuple(
        sorted(result.history, key=lambda point: point.timestamp_ms)
    )
    assert all(
        url.startswith("https://openapi.okx.com/")
        for _, url, _ in transport.requests
    )


def test_malformed_current_interval_boundaries_are_rejected():
    async def handler(method, url, params):
        return FIXTURE["okx"]["malformed_current"]

    with pytest.raises(InvalidResponse, match="interval boundaries"):
        asyncio.run(
            OkxFundingAdapter(StubTransport(handler)).fetch(
                instrument("OKX"), None, include_history=False
            )
        )


def test_malformed_settled_history_preserves_current_snapshot():
    async def handler(method, url, params):
        if url.endswith("funding-rate-history"):
            return FIXTURE["okx"]["malformed_history"]
        return FIXTURE["okx"]["current"]

    result = asyncio.run(
        OkxFundingAdapter(StubTransport(handler)).fetch(
            instrument("OKX"), None, include_history=True
        )
    )

    assert result.current.kind is FundingRateKind.INDICATIVE
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"


def test_documented_hourly_frequency_is_explicit_interval_evidence():
    async def handler(method, url, params):
        return FIXTURE["hyperliquid"]

    result = asyncio.run(
        HyperliquidFundingAdapter(StubTransport(handler), lambda: 1_700_000_000).fetch(
            instrument("HYPERLIQUID"), None, include_history=True
        )
    )

    assert result.current.interval.kind is FundingIntervalKind.EXPLICIT_DURATION
    assert result.current.interval.duration_seconds == 3_600
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


def test_optional_raw_interval_with_explicit_minutes_is_preserved():
    interval = CcxtFundingAdapter._reported_interval(
        FIXTURE["optional"]["raw_interval_minutes"]
    )

    assert interval.kind is FundingIntervalKind.EXPLICIT_DURATION
    assert interval.duration_seconds == 28_800


@pytest.mark.parametrize(
    "value", FIXTURE["optional"]["malformed_interval_minutes"]
)
def test_optional_raw_interval_rejects_invalid_minute_values(value):
    with pytest.raises(InvalidResponse, match="invalid funding interval"):
        CcxtFundingAdapter._reported_interval(
            {"info": {"funding_interval_minutes": value}}
        )


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


def test_optional_history_normalization_failure_preserves_current_snapshot():
    class Exchange:
        has = {
            "fetchFundingRate": True,
            "fetchFundingRateHistory": True,
        }

        async def fetch_funding_rate(self, symbol):
            return {"timestamp": 1_700_000_000_000, "fundingRate": "0.0001"}

        async def fetch_funding_rate_history(self, symbol, *, since, limit):
            return [
                {"timestamp": 1_699_999_998_993, "fundingRate": "0.00008"},
                {"timestamp": 1_700_000_000_000, "fundingRate": "0.00009"},
            ]

    class Adapter(CcxtFundingAdapter):
        async def _market(self, subject):
            return Exchange(), "EXACT-ENDPOINT-ID"

    result = asyncio.run(
        Adapter().fetch(instrument("ASTER"), None, include_history=True)
    )

    assert result.current.sample.rate == Decimal("0.0001")
    assert result.history == ()
    assert result.history_issue is not None
    assert "unambiguous" in result.history_issue.message


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
    assert result.current.interval.kind is FundingIntervalKind.EXPLICIT_DURATION
    assert result.current.interval.duration_seconds == 3_600
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


def test_sparse_full_retained_history_preserves_documented_funding_frequency():
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
        point.interval.kind is FundingIntervalKind.EXPLICIT_DURATION
        for point in result.history
    )
    assert all(point.interval.duration_seconds == 3_600 for point in result.history)


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


@pytest.mark.parametrize(
    ("adapter", "venue", "symbol", "current_kind"),
    [
        (DeepcoinFundingAdapter, "DEEPCOIN", "BASE-QUOTE-SWAP", FundingRateKind.INDICATIVE),
        (KucoinFundingAdapter, "KUCOIN", "BASEQUOTEM", FundingRateKind.INDICATIVE),
        (HtxFundingAdapter, "HTX", "BASE-QUOTE", FundingRateKind.NEXT),
    ],
)
def test_added_native_funding_preserves_current_and_settled_history(
    adapter, venue, symbol, current_kind
):
    fixture = ADDED_VENUES[venue.lower()]

    async def handler(method, url, params):
        is_history = (
            "histor" in url
            or "fund-rate/history" in url
            or "contract/funding-rates" in url
        )
        return fixture["funding_history"] if is_history else fixture["funding_current"]

    result = asyncio.run(
        adapter(StubTransport(handler), lambda: 1_700_000_100).fetch(
            instrument(venue, symbol=symbol),
            HistoryRange(1_699_000_000_000, 1_700_100_000_000),
            include_history=True,
        )
    )

    assert result.current.kind is current_kind
    assert result.current.rate == pytest.approx(0.0001)
    assert result.history[0].kind is FundingRateKind.SETTLED
    assert result.history[0].rate == pytest.approx(0.00008)


@pytest.mark.parametrize(
    ("adapter", "venue", "symbol"),
    [
        (DeepcoinFundingAdapter, "DEEPCOIN", "BASE-QUOTE-SWAP"),
        (KucoinFundingAdapter, "KUCOIN", "BASEQUOTEM"),
        (HtxFundingAdapter, "HTX", "BASE-QUOTE"),
    ],
)
def test_added_native_malformed_funding_is_rejected(adapter, venue, symbol):
    async def handler(method, url, params):
        return ADDED_VENUES[venue.lower()]["malformed"]

    with pytest.raises((InvalidResponse, TypeError)):
        asyncio.run(
            adapter(StubTransport(handler)).fetch(
                instrument(venue, symbol=symbol), None, include_history=False
            )
        )


def test_malformed_native_funding_history_preserves_current_snapshot():
    async def handler(method, url, params):
        return (
            ADDED_VENUES["deepcoin"]["malformed"]
            if url.endswith("fund-rate/history")
            else ADDED_VENUES["deepcoin"]["funding_current"]
        )

    result = asyncio.run(
        DeepcoinFundingAdapter(StubTransport(handler)).fetch(
            instrument("DEEPCOIN", symbol="BASE-QUOTE-SWAP"),
            None,
            include_history=True,
        )
    )

    assert result.current.rate == pytest.approx(0.0001)
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"


@pytest.mark.parametrize(
    ("adapter", "venue", "symbol", "history_fixture"),
    [
        (
            DeepcoinFundingAdapter,
            "DEEPCOIN",
            "BASE-QUOTE-SWAP",
            "funding_history_empty",
        ),
        (
            KucoinFundingAdapter,
            "KUCOIN",
            "BASEQUOTEM",
            "funding_history_empty",
        ),
    ],
)
def test_successful_empty_native_funding_slice_is_not_a_history_failure(
    adapter, venue, symbol, history_fixture
):
    fixture = ADDED_VENUES[venue.lower()]

    async def handler(method, url, params):
        is_history = "fund-rate/history" in url or "contract/funding-rates" in url
        return fixture[history_fixture] if is_history else fixture["funding_current"]

    result = asyncio.run(
        adapter(StubTransport(handler), lambda: 1_700_000_100).fetch(
            instrument(venue, symbol=symbol),
            HistoryRange(1_699_000_000_000, 1_699_100_000_000),
            include_history=True,
        )
    )

    assert result.history == ()
    assert result.history_issue is None


def test_missing_native_funding_history_member_is_reported():
    fixture = ADDED_VENUES["kucoin"]

    async def handler(method, url, params):
        if "contract/funding-rates" in url:
            return {"code": "200000"}
        return fixture["funding_current"]

    result = asyncio.run(
        KucoinFundingAdapter(StubTransport(handler), lambda: 1_700_000_100).fetch(
            instrument("KUCOIN", symbol="BASEQUOTEM"),
            HistoryRange(1_699_000_000_000, 1_699_100_000_000),
            include_history=True,
        )
    )

    assert result.current.rate == pytest.approx(0.0001)
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"


def test_flat_successful_native_funding_envelope_is_accepted():
    fixture = ADDED_VENUES["deepcoin"]

    async def handler(method, url, params):
        return (
            fixture["funding_history_flat"]
            if url.endswith("fund-rate/history")
            else fixture["funding_current"]
        )

    result = asyncio.run(
        DeepcoinFundingAdapter(StubTransport(handler), lambda: 1_700_000_100).fetch(
            instrument("DEEPCOIN", symbol="BASE-QUOTE-SWAP"),
            HistoryRange(1_699_000_000_000, 1_700_100_000_000),
            include_history=True,
        )
    )

    assert [point.sample.rate for point in result.history] == [Decimal("0.00008")]
    assert result.history_issue is None
