from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cdm import (
    AssetRelationship,
    ContractValueDescriptorV1,
    ContractValueUnit,
    DataPointKind,
    FundingRateKind,
    InstrumentDescriptorV1,
    InstrumentKind,
    InstrumentReferenceV1,
    NativeIdentityNamespace,
    NativeIdentityRole,
    NativeIdentityV1,
    SettlementDescriptorV1,
    TemporalMode,
)

import perp_md.adapters.ccxt as ccxt_module
from perp_md import (
    CapabilityStatus,
    CapabilityUnavailable,
    FundingClient,
    HistoryScope,
    OpenInterestCapabilities,
    OpenInterestClient,
    PaginationMode,
    RequestScope,
    assess_capability,
)
from perp_md.adapters.ccxt import CcxtAdapter
from perp_md.adapters.ccxt_funding import CcxtFundingAdapter
from perp_md.capabilities import (
    CCXT_FUNDING_FEATURE,
    CCXT_FUNDING_HISTORY_FEATURE,
    CCXT_OPEN_INTEREST_FEATURE,
    CCXT_OPEN_INTEREST_HISTORY_FEATURE,
)
from perp_md.models import FundingCapabilities


class StubTransport:
    def __init__(self, handler=None):
        self.handler = handler
        self.requests = []

    async def get(self, url, params=None):
        self.requests.append(("GET", url, params))
        return await self.handler("GET", url, params)

    async def post(self, url, payload):
        self.requests.append(("POST", url, payload))
        return await self.handler("POST", url, payload)

    async def close(self):
        return None


def reference(
    direction: str = "linear",
    *identities: NativeIdentityV1,
    amount: str = "1",
    kind: InstrumentKind = InstrumentKind.PERPETUAL_SWAP,
) -> InstrumentReferenceV1:
    return InstrumentReferenceV1(
        InstrumentDescriptorV1(
            instrument_kind=kind,
            contract_value=ContractValueDescriptorV1(
                amount=Decimal(amount),
                unit=(
                    ContractValueUnit.BASE
                    if direction == "linear"
                    else ContractValueUnit.QUOTE
                ),
            ),
            settlement=SettlementDescriptorV1(
                asset_relationship=(
                    AssetRelationship.QUOTE
                    if direction == "linear"
                    else AssetRelationship.BASE
                )
            ),
        ),
        identities,
    )


def native_identity(
    role: NativeIdentityRole,
    namespace: NativeIdentityNamespace,
    value: str,
) -> NativeIdentityV1:
    return NativeIdentityV1(role, namespace, value)


def test_runtime_probe_checks_methods_instead_of_module_presence(monkeypatch):
    exchange = SimpleNamespace(
        has={
            "fetchOpenInterest": False,
            "fetchOpenInterestHistory": True,
        },
    )
    closed = False

    async def close():
        nonlocal closed
        closed = True

    exchange.close = close
    monkeypatch.setattr(
        ccxt_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(deribit=lambda config: exchange),
    )
    adapter = CcxtAdapter()
    subject = reference(
        "linear",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST,
            "EXACT-NATIVE-ID",
        ),
    )

    features = asyncio.run(
        adapter.runtime_features(SimpleNamespace(venue="DERIBIT", reference=subject))
    )

    assert CCXT_OPEN_INTEREST_FEATURE not in features
    assert CCXT_OPEN_INTEREST_HISTORY_FEATURE in features
    assert closed is True


def test_funding_runtime_probe_reports_only_proven_provider_methods(monkeypatch):
    exchange = SimpleNamespace(
        has={
            "fetchFundingRate": True,
            "fetchFundingRateHistory": False,
        },
    )

    async def close():
        return None

    exchange.close = close
    monkeypatch.setattr(
        ccxt_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(deribit=lambda config: exchange),
    )

    features = asyncio.run(
        CcxtFundingAdapter().runtime_features(SimpleNamespace(venue="DERIBIT"))
    )

    assert features == frozenset({CCXT_FUNDING_FEATURE})


def test_runtime_assessment_hides_adapter_feature_identifiers_from_consumers():
    class Adapter:
        probes = 0

        def supports(self, subject):
            return True

        async def runtime_features(self, subject):
            self.probes += 1
            return frozenset({CCXT_OPEN_INTEREST_FEATURE})

        async def close(self):
            return None

    subject = reference(
        "linear",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST,
            "EXACT-NATIVE-ID",
        ),
    )
    adapter = Adapter()
    client = OpenInterestClient(
        adapters={"DERIBIT": adapter},
        transport=StubTransport(),
    )

    assessment = asyncio.run(
        client.assess_runtime(
            "DERIBIT",
            subject,
            temporal_mode=TemporalMode.CURRENT,
        )
    )
    repeated = asyncio.run(
        client.assess_runtime(
            "DERIBIT",
            subject,
            temporal_mode=TemporalMode.CURRENT,
        )
    )

    assert assessment.status is CapabilityStatus.SUPPORTED
    assert repeated.status is CapabilityStatus.SUPPORTED
    assert adapter.probes == 1


def test_missing_history_identity_is_structured_before_network_acquisition():
    transport = StubTransport()
    client = OpenInterestClient(transport=transport)
    subject = reference(
        "inverse",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST,
            "EXACT-CONTRACT-ID",
        ),
        amount="10",
    )

    with pytest.raises(CapabilityUnavailable) as raised:
        asyncio.run(client.fetch_reference("BINANCE", subject))

    assert raised.value.assessment.status is CapabilityStatus.METADATA_INCOMPLETE
    assert raised.value.assessment.issues[0].code == "missing_native_identity"
    assert transport.requests == []


def test_ambiguous_identity_is_reported_without_order_based_selection():
    subject = reference(
        "inverse",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST,
            "EXACT-CONTRACT-ID",
        ),
        native_identity(
            NativeIdentityRole.PAIR,
            NativeIdentityNamespace.REST,
            "FIRST-PAIR-ID",
        ),
        native_identity(
            NativeIdentityRole.PAIR,
            NativeIdentityNamespace.REST,
            "SECOND-PAIR-ID",
        ),
        amount="10",
    )

    assessment = assess_capability(
        "BINANCE",
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        subject,
        temporal_mode=TemporalMode.HISTORICAL,
    )

    assert assessment.status is CapabilityStatus.METADATA_INCOMPLETE
    assert assessment.issues[0].code == "ambiguous_native_identity"


def test_reference_history_passes_the_exact_pair_identity_without_rewriting():
    async def handler(method, url, params):
        if url.endswith("openInterestHist"):
            assert params["pair"] == "EXACT-PAIR-ID"
            return []
        if url.endswith("openInterest"):
            return {"openInterest": "1", "time": 1_700_000_000_000}
        return {"markPrice": "2"}

    subject = reference(
        "inverse",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST,
            "EXACT-CONTRACT-ID",
        ),
        native_identity(
            NativeIdentityRole.PAIR,
            NativeIdentityNamespace.REST,
            "EXACT-PAIR-ID",
        ),
        amount="10",
    )
    client = OpenInterestClient(transport=StubTransport(handler))

    result = asyncio.run(client.fetch_reference("BINANCE", subject))

    assert result.current.value_usd == 10
    assert result.history == ()


def test_dated_inverse_history_passes_exact_pair_and_contract_type_identities():
    async def handler(method, url, params):
        if url.endswith("openInterestHist"):
            assert params["pair"] == "EXACT-PAIR-ID"
            assert params["contractType"] == "NEXT_QUARTER"
            return [
                {
                    "timestamp": 1_699_999_800_000,
                    "sumOpenInterest": "2",
                }
            ]
        if url.endswith("openInterest"):
            return {"openInterest": "1", "time": 1_700_000_000_000}
        return {"markPrice": "2"}

    subject = reference(
        "inverse",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST,
            "EXACT-CONTRACT-ID",
        ),
        native_identity(
            NativeIdentityRole.PAIR,
            NativeIdentityNamespace.REST,
            "EXACT-PAIR-ID",
        ),
        native_identity(
            NativeIdentityRole.PRODUCT_FAMILY,
            NativeIdentityNamespace.REST,
            "NEXT_QUARTER",
        ),
        amount="10",
        kind=InstrumentKind.FUTURE,
    )
    client = OpenInterestClient(transport=StubTransport(handler))

    result = asyncio.run(client.fetch_reference("BINANCE", subject))

    assert result.current.value_usd == 10
    assert result.history[0].value_usd == 20


def test_dated_inverse_history_requires_explicit_rest_contract_category():
    subject = reference(
        "inverse",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST,
            "EXACT-CONTRACT-ID",
        ),
        native_identity(
            NativeIdentityRole.PAIR,
            NativeIdentityNamespace.REST,
            "EXACT-PAIR-ID",
        ),
        amount="10",
        kind=InstrumentKind.FUTURE,
    )
    client = OpenInterestClient(transport=StubTransport())

    with pytest.raises(CapabilityUnavailable) as raised:
        asyncio.run(client.fetch_reference("BINANCE", subject))

    assert raised.value.assessment.status is CapabilityStatus.METADATA_INCOMPLETE
    assert raised.value.assessment.issues[0].code == "missing_native_identity"


def test_scoped_aggregate_uses_separate_exact_route_and_instrument_identities():
    async def handler(method, url, payload):
        assert payload == {"type": "metaAndAssetCtxs", "dex": "EXACT-SCOPE-ID"}
        return [
            {"universe": [{"name": "EXACT-SCOPED-INSTRUMENT-ID"}]},
            [{"openInterest": "3", "markPx": "2"}],
        ]

    transport = StubTransport(handler)
    client = OpenInterestClient(transport=transport)
    subject = reference(
        "linear",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.RPC,
            "EXACT-SCOPED-INSTRUMENT-ID",
        ),
        native_identity(
            NativeIdentityRole.PRODUCT_FAMILY,
            NativeIdentityNamespace.RPC,
            "EXACT-SCOPE-ID",
        ),
    )

    result = asyncio.run(
        client.fetch_reference("HYPERLIQUID", subject, include_history=False)
    )

    assert result.current.value_usd == 6


def test_specialized_catalog_fetch_requires_only_its_declared_identity(monkeypatch):
    exchanges = []

    class Exchange:
        has = {}

        def __init__(self):
            self.closed = False
            exchanges.append(self)

        async def v1_public_get_instruments(self):
            return [
                {
                    "symbol": "EXACT-CATALOG-ID",
                    "open_interest": "3",
                    "base_asset_multiplier": "1",
                    "quote": {
                        "mark_price": "2",
                        "timestamp": "2026-08-10T00:00:00Z",
                    },
                }
            ]

        async def close(self):
            self.closed = True

    monkeypatch.setattr(
        ccxt_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(coinbaseinternational=lambda config: Exchange()),
    )
    subject = reference(
        "linear",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST_INSTRUMENT_CATALOG,
            "EXACT-CATALOG-ID",
        ),
    )
    client = OpenInterestClient(
        transport=StubTransport(),
        enable_ccxt_fallback=True,
    )

    result = asyncio.run(
        client.fetch_reference("COINBASE", subject, include_history=False)
    )

    assert result.current.value_usd == 6
    assert exchanges and all(exchange.closed for exchange in exchanges)


def test_funding_reference_api_accepts_the_cdm_envelope_unchanged():
    async def handler(method, url, params):
        return {"lastFundingRate": "0.001", "time": 1_700_000_000_000}

    subject = reference(
        "linear",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST,
            "EXACT-NATIVE-ID",
        ),
    )
    client = FundingClient(transport=StubTransport(handler))

    result = asyncio.run(
        client.fetch_reference("BINANCE", subject, include_history=False)
    )

    assert result.current.sample.rate == Decimal("0.001")


def test_operation_plan_exposes_fixed_history_constraints_without_route_details():
    subject = reference(
        "linear",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST,
            "EXACT-NATIVE-ID",
        ),
    )
    client = OpenInterestClient(transport=StubTransport())

    plan = asyncio.run(
        client.plan_reference(
            "BINANCE",
            subject,
            datapoint=DataPointKind.OPEN_INTEREST_NOTIONAL,
            temporal_mode=TemporalMode.HISTORICAL,
        )
    )

    assert plan.status is CapabilityStatus.SUPPORTED
    assert plan.retrieval is not None
    assert plan.retrieval.request_scope is RequestScope.INSTRUMENT
    assert plan.retrieval.history_scope is HistoryScope.BOUNDED
    assert plan.retrieval.pagination is PaginationMode.TIME_CURSOR
    assert plan.retrieval.fixed_interval_seconds == 300
    assert plan.retrieval.max_lookback_seconds == 30 * 86_400
    assert plan.retrieval.requires_explicit_start is False


def test_operation_plan_preserves_explicit_start_and_documented_frequency():
    subject = reference(
        "linear",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST,
            "EXACT-NATIVE-ID",
        ),
    )
    client = FundingClient(transport=StubTransport())

    plan = asyncio.run(
        client.plan_reference(
            "KRAKEN",
            subject,
            datapoint=DataPointKind.FUNDING_SETTLED_RATE,
            temporal_mode=TemporalMode.HISTORICAL,
        )
    )

    assert plan.status is CapabilityStatus.SUPPORTED
    assert plan.retrieval is not None
    assert plan.retrieval.history_scope is HistoryScope.FULL_RETAINED
    assert plan.retrieval.pagination is PaginationMode.FULL_DOWNLOAD
    assert plan.retrieval.fixed_interval_seconds == 3_600
    assert plan.retrieval.max_lookback_seconds is None
    assert plan.retrieval.requires_explicit_start is True


def test_optional_runtime_plan_returns_generic_constraints_without_feature_ids():
    class Adapter:
        def supports(self, subject):
            return True

        def capabilities(self, subject):
            return OpenInterestCapabilities(True, True, 3_600, 8)

        async def runtime_features(self, subject):
            return frozenset(
                {
                    CCXT_OPEN_INTEREST_FEATURE,
                    CCXT_OPEN_INTEREST_HISTORY_FEATURE,
                }
            )

        async def close(self):
            return None

    subject = reference(
        "linear",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST,
            "EXACT-NATIVE-ID",
        ),
    )
    client = OpenInterestClient(
        adapters={"BITMART": Adapter()},
        transport=StubTransport(),
    )

    plan = asyncio.run(
        client.plan_reference(
            "BITMART",
            subject,
            datapoint=DataPointKind.OPEN_INTEREST_NOTIONAL,
            temporal_mode=TemporalMode.HISTORICAL,
        )
    )

    assert plan.status is CapabilityStatus.SUPPORTED
    assert plan.retrieval is not None
    assert plan.retrieval.pagination is PaginationMode.RUNTIME_DEFINED
    assert plan.retrieval.fixed_interval_seconds == 3_600
    assert plan.retrieval.max_lookback_seconds == 8 * 86_400
    assert "ccxt.fetch" not in repr(plan)


def test_native_product_mapping_selects_bounded_historical_funding_cursor():
    class Adapter:
        def supports(self, subject):
            return True

        def capabilities(self, subject):
            return FundingCapabilities(
                True,
                (FundingRateKind.NEXT,),
                True,
            )

        async def runtime_features(self, subject):
            return frozenset()

        async def close(self):
            return None

    subject = reference(
        "linear",
        native_identity(
            NativeIdentityRole.INSTRUMENT,
            NativeIdentityNamespace.REST,
            "EXACT-NATIVE-ID",
        ),
    )
    client = FundingClient(
        adapters={"XT": Adapter()},
        transport=StubTransport(),
    )

    plan = asyncio.run(
        client.plan_reference(
            "XT",
            subject,
            datapoint=DataPointKind.FUNDING_SETTLED_RATE,
            temporal_mode=TemporalMode.HISTORICAL,
        )
    )

    assert plan.status is CapabilityStatus.SUPPORTED
    assert plan.retrieval is not None
    assert plan.retrieval.pagination is PaginationMode.TIME_CURSOR
    assert "ccxt.fetch" not in repr(plan)


def test_operation_plan_with_a_metadata_gap_does_not_claim_retrieval_constraints():
    client = OpenInterestClient(transport=StubTransport())

    plan = asyncio.run(
        client.plan_reference(
            "BINANCE",
            reference("linear"),
            datapoint=DataPointKind.OPEN_INTEREST_NOTIONAL,
            temporal_mode=TemporalMode.HISTORICAL,
        )
    )

    assert plan.status is CapabilityStatus.METADATA_INCOMPLETE
    assert plan.issues[0].code == "missing_native_identity"
    assert plan.retrieval is None
