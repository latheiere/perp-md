from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from perp_md import (
    AdapterUnavailable,
    ContractDirection,
    DataUnavailable,
    HistoryRange,
    Instrument,
    InvalidInstrument,
    InvalidResponse,
    NativeUnit,
    OpenInterestClient,
    ValuationMethod,
)
from perp_md.adapters.ccxt import CcxtAdapter, resolve_ccxt_symbol
from perp_md.adapters.native import (
    BinanceAdapter,
    BybitAdapter,
    GateAdapter,
    HyperliquidAdapter,
    MexcAdapter,
    OkxAdapter,
)
from perp_md.transport import HttpxTransport
import perp_md.adapters.native as native
import perp_md.adapters.ccxt as ccxt_adapter_module


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "native_open_interest.json").read_text()
)
MEXC_SUCCESS = json.loads(
    (Path(__file__).parent / "fixtures" / "mexc_ticker_success.json").read_text()
)
MEXC_MALFORMED = json.loads(
    (Path(__file__).parent / "fixtures" / "mexc_ticker_malformed.json").read_text()
)
MEXC_REJECTED = json.loads(
    (Path(__file__).parent / "fixtures" / "mexc_ticker_rejected.json").read_text()
)
CCXT_HOURLY = json.loads(
    (Path(__file__).parent / "fixtures" / "ccxt_hourly_open_interest.json").read_text()
)
HYPERLIQUID_CONTEXTS = json.loads(
    (Path(__file__).parent / "fixtures" / "hyperliquid_meta_contexts.json").read_text()
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


class StubCcxtExchange:
    def __init__(self, history, *, supports_history=True):
        self.has = {
            "fetchOpenInterest": True,
            "fetchOpenInterestHistory": supports_history,
        }
        self.markets_by_id = {
            CCXT_HOURLY["market_id"]: [{
                "symbol": CCXT_HOURLY["symbol"],
                "contract": True,
            }]
        }
        self.history = history
        self.history_requests: list[dict[str, Any]] = []

    async def fetch_open_interest(self, symbol):
        assert symbol == CCXT_HOURLY["symbol"]
        return CCXT_HOURLY["current"]

    async def fetch_open_interest_history(self, symbol, *, timeframe, since, limit):
        self.history_requests.append({
            "symbol": symbol,
            "timeframe": timeframe,
            "since": since,
            "limit": limit,
        })
        return self.history

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


def test_binance_pages_backward_deduplicates_and_keeps_current(monkeypatch):
    pages = FIXTURE["binance"]["history"]
    monkeypatch.setattr(native, "BINANCE_HISTORY_LIMIT", 2)

    async def handler(method, url, params):
        if url.endswith("openInterestHist"):
            return pages[0 if params["endTime"] == 900_000 else 1]
        if url.endswith("openInterest"):
            return FIXTURE["binance"]["current"]
        return FIXTURE["binance"]["mark"]

    transport = StubTransport(handler)
    result = asyncio.run(BinanceAdapter(transport, lambda: 900.5).fetch(
        instrument("BINANCE"), HistoryRange(300_000, 900_000), include_history=True
    ))
    assert result.current.value_usd == 20
    assert [row.timestamp_ms for row in result.history] == [300_000, 600_000, 900_000]
    requests = [params for _, url, params in transport.requests if url.endswith("openInterestHist")]
    assert [row["endTime"] for row in requests] == [900_000, 599_999]


def test_history_failure_is_structured_partial_success():
    async def handler(method, url, params):
        if url.endswith("openInterestHist"):
            raise RuntimeError("history unavailable")
        if url.endswith("openInterest"):
            return FIXTURE["binance"]["current"]
        return FIXTURE["binance"]["mark"]

    result = asyncio.run(BinanceAdapter(StubTransport(handler), lambda: 900.5).fetch(
        instrument("BINANCE"), HistoryRange(300_000, 900_000), include_history=True
    ))
    assert result.current.value_usd == 20
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"


def test_bybit_follows_cursor_and_marks_historical_valuation():
    pages = FIXTURE["bybit"]["history"]

    async def handler(method, url, params):
        if url.endswith("open-interest"):
            return pages[1 if params.get("cursor") else 0]
        return FIXTURE["bybit"]["ticker"]

    transport = StubTransport(handler)
    result = asyncio.run(BybitAdapter(transport, lambda: 900.5).fetch(
        instrument("BYBIT"), HistoryRange(300_000, 900_000), include_history=True
    ))
    assert result.current.value_usd == 18
    assert [row.value_usd for row in result.history] == [6, 12, 18]
    assert all(row.valuation is ValuationMethod.CURRENT_MARK for row in result.history)
    history_requests = [params for _, url, params in transport.requests if url.endswith("open-interest")]
    assert history_requests[1]["cursor"] == "next"


def test_gate_current_includes_both_position_sides():
    async def handler(method, url, params):
        if url.endswith("contract_stats"):
            return FIXTURE["gate"]["history"]
        return FIXTURE["gate"]["details"]

    result = asyncio.run(GateAdapter(StubTransport(handler), lambda: 900.5).fetch(
        instrument("GATE"), None, include_history=True
    ))
    assert result.current.native_value == 10
    assert result.current.value_usd == 10
    assert result.history[0].value_usd == 8


def test_gate_continues_after_a_short_sparse_history_page(monkeypatch):
    pages = FIXTURE["gate"]["sparse_history"]
    monkeypatch.setattr(native, "GATE_HISTORY_LIMIT", 3)

    async def handler(method, url, params):
        if url.endswith("contract_stats"):
            return pages[0 if params["from"] == 300 else 1]
        return FIXTURE["gate"]["details"]

    transport = StubTransport(handler)
    result = asyncio.run(GateAdapter(transport, lambda: 1_500.5).fetch(
        instrument("GATE"), HistoryRange(300_000, 1_500_000), include_history=True
    ))

    assert [row.timestamp_ms for row in result.history] == [
        300_000, 900_000, 1_200_000, 1_500_000,
    ]
    requests = [
        params for _, url, params in transport.requests
        if url.endswith("contract_stats")
    ]
    assert [row["from"] for row in requests] == [300, 901]


def test_gate_malformed_history_preserves_current_observation():
    async def handler(method, url, params):
        if url.endswith("contract_stats"):
            return FIXTURE["gate"]["malformed_history"]
        return FIXTURE["gate"]["details"]

    result = asyncio.run(GateAdapter(StubTransport(handler), lambda: 900.5).fetch(
        instrument("GATE"), HistoryRange(300_000, 900_000), include_history=True
    ))

    assert result.current.value_usd == 10
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"


def test_okx_preserves_reported_zero():
    async def handler(method, url, params):
        return FIXTURE["okx"]

    result = asyncio.run(OkxAdapter(StubTransport(handler)).fetch(
        instrument("OKX"), None, include_history=False
    ))
    assert result.current.value_usd == 0


@pytest.mark.parametrize("product", [None, "PERP"], ids=["no-product", "ordinary-product"])
def test_default_perpetual_universe_retains_unscoped_request_behavior(product):
    payload = HYPERLIQUID_CONTEXTS["default_success"]

    async def handler(method, url, params):
        return payload

    transport = StubTransport(handler)
    adapter = HyperliquidAdapter(transport, lambda: 1)
    subject = instrument(
        "HYPERLIQUID",
        symbol=payload[0]["universe"][0]["name"],
        product=product,
    )
    result = asyncio.run(adapter.fetch(
        subject, None, include_history=False
    ))

    context = payload[1][0]
    assert result.current.native_unit is NativeUnit.BASE
    assert result.current.value_usd == pytest.approx(
        float(context["openInterest"]) * float(context["markPx"])
    )
    assert transport.requests == [
        ("POST", "https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"})
    ]
    capabilities = adapter.capabilities(subject)
    assert capabilities.current is True
    assert capabilities.history is False


@pytest.mark.parametrize(
    ("symbol_form", "product_form"),
    [
        ("qualified", None),
        ("local", "descriptor"),
        ("qualified", "descriptor"),
    ],
    ids=[
        "symbol-namespace",
        "prefixed-product-descriptor",
        "matching-symbol-and-product",
    ],
)
def test_scoped_perpetual_universe_uses_native_identity(symbol_form, product_form):
    payload = HYPERLIQUID_CONTEXTS["scoped_success"]
    qualified_symbol = payload[0]["universe"][0]["name"]
    scope, local_symbol = qualified_symbol.split(":", 1)

    async def handler(method, url, params):
        return payload

    transport = StubTransport(handler)
    product = None
    if product_form == "descriptor":
        product = f"HIP-3:{scope}"
    subject = instrument(
        "HYPERLIQUID",
        symbol=qualified_symbol if symbol_form == "qualified" else local_symbol,
        product=product,
    )
    result = asyncio.run(HyperliquidAdapter(transport, lambda: 1).fetch(
        subject, None, include_history=False
    ))

    context = payload[1][0]
    assert result.current.native_value == float(context["openInterest"])
    assert result.current.mark_price == float(context["markPx"])
    assert transport.requests == [
        (
            "POST",
            "https://api.hyperliquid.xyz/info",
            {"type": "metaAndAssetCtxs", "dex": scope},
        )
    ]


@pytest.mark.parametrize(
    ("fixture_name", "error"),
    [("malformed", InvalidResponse), ("absent", DataUnavailable)],
)
def test_perpetual_universe_rejects_malformed_or_absent_observations(fixture_name, error):
    payload = HYPERLIQUID_CONTEXTS[fixture_name]

    async def handler(method, url, params):
        return payload

    symbol = (
        payload[0]["universe"][0]["name"]
        if fixture_name == "malformed"
        else "ABSENT_NATIVE_SYMBOL"
    )
    with pytest.raises(error):
        asyncio.run(HyperliquidAdapter(StubTransport(handler), lambda: 1).fetch(
            instrument("HYPERLIQUID", symbol=symbol), None, include_history=False
        ))


def test_scoped_perpetual_identity_rejects_conflicting_native_scope():
    payload = HYPERLIQUID_CONTEXTS["scoped_success"]
    qualified_symbol = payload[0]["universe"][0]["name"]

    with pytest.raises(InvalidInstrument):
        asyncio.run(HyperliquidAdapter(StubTransport(None), lambda: 1).fetch(
            instrument(
                "HYPERLIQUID",
                symbol=qualified_symbol,
                product="HIP-3:conflicting-scope",
            ),
            None,
            include_history=False,
        ))


@pytest.mark.parametrize(
    "product_template",
    [
        "",
        "HIP-3",
        "HIP-3:",
        "HIP-3:{scope}:extra",
        "OTHER:{scope}",
        " {scope}",
    ],
    ids=[
        "empty",
        "missing-descriptor-scope",
        "empty-descriptor-scope",
        "multi-part-descriptor-scope",
        "unsupported-descriptor-family",
        "whitespace",
    ],
)
def test_scoped_perpetual_identity_rejects_malformed_product(product_template):
    payload = HYPERLIQUID_CONTEXTS["scoped_success"]
    qualified_symbol = payload[0]["universe"][0]["name"]
    scope, local_symbol = qualified_symbol.split(":", 1)
    product = product_template.format(scope=scope)

    with pytest.raises(InvalidInstrument):
        asyncio.run(HyperliquidAdapter(StubTransport(None), lambda: 1).fetch(
            instrument("HYPERLIQUID", symbol=local_symbol, product=product),
            None,
            include_history=False,
        ))


def test_aggregate_contract_ticker_uses_exact_symbol_and_generic_linear_metadata():
    async def handler(method, url, params):
        return MEXC_SUCCESS

    selected = MEXC_SUCCESS["data"][1]
    transport = StubTransport(handler)
    adapter = MexcAdapter(transport)
    subject = instrument(
        "MEXC",
        symbol=selected["symbol"],
        contract_multiplier=0.01,
    )
    result = asyncio.run(adapter.fetch(subject, None, include_history=True))

    assert result.current.timestamp_ms == selected["timestamp"]
    assert result.current.native_value == selected["holdVol"]
    assert result.current.native_unit is NativeUnit.CONTRACTS
    assert result.current.mark_price == selected["fairPrice"]
    assert result.current.value_usd == pytest.approx(
        selected["holdVol"] * 0.01 * selected["fairPrice"]
    )
    assert result.current.valuation is ValuationMethod.MARK_PRICE
    assert result.history == ()
    assert result.history_issue is None
    assert transport.requests == [
        ("GET", "https://contract.mexc.com/api/v1/contract/ticker", None)
    ]

    capabilities = adapter.capabilities(subject)
    assert capabilities.current is True
    assert capabilities.history is False
    assert capabilities.required_metadata == (
        "contract_direction",
        "contract_multiplier",
    )
    registered = OpenInterestClient(transport=transport).capabilities(subject)
    assert registered == capabilities


@pytest.mark.parametrize("payload", [MEXC_MALFORMED, MEXC_REJECTED])
def test_aggregate_contract_ticker_rejects_invalid_payloads(payload):
    async def handler(method, url, params):
        return payload

    symbol = MEXC_MALFORMED["data"][0]["symbol"]
    with pytest.raises(InvalidResponse):
        asyncio.run(MexcAdapter(StubTransport(handler)).fetch(
            instrument("MEXC", symbol=symbol), None, include_history=False
        ))


@pytest.mark.parametrize("ambiguous", [False, True])
def test_aggregate_contract_ticker_requires_one_exact_instrument_match(ambiguous):
    selected = MEXC_SUCCESS["data"][0]
    payload = MEXC_SUCCESS
    symbol = "ABSENT_NATIVE_SYMBOL"
    if ambiguous:
        payload = {**MEXC_SUCCESS, "data": [*MEXC_SUCCESS["data"], dict(selected)]}
        symbol = selected["symbol"]

    async def handler(method, url, params):
        return payload

    with pytest.raises(DataUnavailable):
        asyncio.run(MexcAdapter(StubTransport(handler)).fetch(
            instrument("MEXC", symbol=symbol), None, include_history=False
        ))


def test_concurrent_aggregate_contract_ticker_reads_share_one_request():
    calls = 0

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return MEXC_SUCCESS

    class Client:
        async def get(self, url, params=None):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return Response()

        async def aclose(self):
            return None

    async def scenario():
        transport = HttpxTransport()
        transport._http = Client()
        adapter = MexcAdapter(transport)
        subjects = [
            instrument(
                "MEXC",
                symbol=row["symbol"],
                contract_multiplier=multiplier,
            )
            for row, multiplier in zip(MEXC_SUCCESS["data"], (0.0001, 0.01))
        ]
        results = await asyncio.gather(*(
            adapter.fetch(subject, None, include_history=False)
            for subject in subjects
        ))
        await transport.close()
        return results

    assert len(asyncio.run(scenario())) == 2
    assert calls == 1


def test_client_requires_an_explicitly_available_adapter():
    client = OpenInterestClient(transport=StubTransport(None), adapters={})
    with pytest.raises(AdapterUnavailable):
        client.capabilities(instrument("UNKNOWN"))


def test_ccxt_symbol_resolution_requires_a_unique_contract():
    class Exchange:
        markets_by_id = {"BASEQUOTE": [{"symbol": "BASE/QUOTE:QUOTE", "contract": True}]}

    assert resolve_ccxt_symbol(Exchange(), instrument("VENUE")) == "BASE/QUOTE:QUOTE"


def configured_ccxt_adapter(monkeypatch, exchange):
    monkeypatch.setattr(
        ccxt_adapter_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(htx=object),
    )
    adapter = CcxtAdapter()
    adapter.exchanges["HTX"] = exchange
    return adapter


def test_ccxt_hourly_history_capabilities_are_venue_supported():
    capabilities = CcxtAdapter().capabilities(instrument("HTX"))

    assert capabilities.current is True
    assert capabilities.history is True
    assert capabilities.history_interval_seconds == 3_600
    assert capabilities.max_history_days == 8


def test_ccxt_hourly_history_uses_supported_cadence_and_source_timestamps(monkeypatch):
    exchange = StubCcxtExchange(CCXT_HOURLY["history"])
    adapter = configured_ccxt_adapter(monkeypatch, exchange)
    first, last = CCXT_HOURLY["history"]
    requested = HistoryRange(first["timestamp"], last["timestamp"])

    result = asyncio.run(adapter.fetch(
        instrument("HTX", symbol=CCXT_HOURLY["market_id"]),
        requested,
        include_history=True,
    ))

    assert result.current.value_usd == CCXT_HOURLY["current"]["openInterestValue"]
    assert [row.timestamp_ms for row in result.history] == [
        first["timestamp"],
        last["timestamp"],
    ]
    assert last["timestamp"] - first["timestamp"] == 3_600_000
    assert [row.value_usd for row in result.history] == [
        first["openInterestValue"],
        last["openInterestValue"],
    ]
    assert all(row.valuation is ValuationMethod.VENUE_REPORTED for row in result.history)
    assert exchange.history_requests == [{
        "symbol": CCXT_HOURLY["symbol"],
        "timeframe": "1h",
        "since": first["timestamp"],
        "limit": 200,
    }]


def test_ccxt_malformed_hourly_history_preserves_current(monkeypatch):
    exchange = StubCcxtExchange(CCXT_HOURLY["malformed_history"])
    adapter = configured_ccxt_adapter(monkeypatch, exchange)

    result = asyncio.run(adapter.fetch(
        instrument("HTX", symbol=CCXT_HOURLY["market_id"]),
        None,
        include_history=True,
    ))

    assert result.current.value_usd == CCXT_HOURLY["current"]["openInterestValue"]
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"
    assert "InvalidResponse" in result.history_issue.message


def test_ccxt_runtime_without_hourly_history_preserves_current(monkeypatch):
    exchange = StubCcxtExchange([], supports_history=False)
    adapter = configured_ccxt_adapter(monkeypatch, exchange)

    result = asyncio.run(adapter.fetch(
        instrument("HTX", symbol=CCXT_HOURLY["market_id"]),
        None,
        include_history=True,
    ))

    assert result.current.value_usd == CCXT_HOURLY["current"]["openInterestValue"]
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"
    assert exchange.history_requests == []
