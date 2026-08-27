from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import perp_md.adapters.ccxt as ccxt_adapter_module
import perp_md.adapters.native as native
from perp_md import (
    AdapterUnavailable,
    ContractDirection,
    DataUnavailable,
    HistoryRange,
    Instrument,
    InvalidInstrument,
    InvalidResponse,
    NativeUnit,
    ObservationTimeKind,
    OpenInterestClient,
    ValuationMethod,
)
from perp_md.adapters.ccxt import CcxtAdapter, resolve_ccxt_symbol
from perp_md.adapters.native import (
    BinanceAdapter,
    BitfinexAdapter,
    BtseAdapter,
    BybitAdapter,
    DeepcoinAdapter,
    GateAdapter,
    HtxAdapter,
    HyperliquidAdapter,
    KrakenAdapter,
    KucoinAdapter,
    LighterAdapter,
    MexcAdapter,
    OkxAdapter,
    PhemexAdapter,
    GrvtAdapter,
    ToobitAdapter,
    XtAdapter,
)
from perp_md.transport import HttpxTransport

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
KRAKEN_OPEN_INTEREST = json.loads(
    (Path(__file__).parent / "fixtures" / "kraken_open_interest.json").read_text()
)
ADDED_VENUES = json.loads(
    (Path(__file__).parent / "fixtures" / "native_added_venues.json").read_text()
)


def test_ccxt_market_initialization_closes_exchange_when_cancelled(monkeypatch):
    started = asyncio.Event()
    closed = False

    class Exchange:
        async def load_markets(self):
            started.set()
            await asyncio.Event().wait()

        async def close(self):
            nonlocal closed
            closed = True

    exchange = Exchange()
    monkeypatch.setattr(
        ccxt_adapter_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(xt=lambda _config: exchange),
    )

    async def scenario():
        adapter = CcxtAdapter()
        task = asyncio.create_task(adapter._market(instrument("XT")))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert closed is True


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
            CCXT_HOURLY["market_id"]: [
                {
                    "symbol": CCXT_HOURLY["symbol"],
                    "contract": True,
                }
            ]
        }
        self.history = history
        self.history_requests: list[dict[str, Any]] = []

    async def fetch_open_interest(self, symbol):
        assert symbol == CCXT_HOURLY["symbol"]
        return CCXT_HOURLY["current"]

    async def fetch_open_interest_history(self, symbol, *, timeframe, since, limit):
        self.history_requests.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "since": since,
                "limit": limit,
            }
        )
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
    result = asyncio.run(
        BinanceAdapter(transport, lambda: 900.5).fetch(
            instrument("BINANCE"), HistoryRange(300_000, 900_000), include_history=True
        )
    )
    assert result.current.value_usd == 20
    assert result.current.base_quantity is not None
    assert float(result.current.base_quantity.amount) == 10
    assert [row.timestamp_ms for row in result.history] == [300_000, 600_000, 900_000]
    assert [
        float(row.base_quantity.amount) for row in result.history if row.base_quantity
    ] == [
        3,
        6,
        9,
    ]
    requests = [
        params
        for _, url, params in transport.requests
        if url.endswith("openInterestHist")
    ]
    assert [row["endTime"] for row in requests] == [900_000, 599_999]


def test_history_failure_is_structured_partial_success():
    async def handler(method, url, params):
        if url.endswith("openInterestHist"):
            raise RuntimeError("history unavailable")
        if url.endswith("openInterest"):
            return FIXTURE["binance"]["current"]
        return FIXTURE["binance"]["mark"]

    result = asyncio.run(
        BinanceAdapter(StubTransport(handler), lambda: 900.5).fetch(
            instrument("BINANCE"), HistoryRange(300_000, 900_000), include_history=True
        )
    )
    assert result.current.value_usd == 20
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"


def test_linear_contract_history_joins_exact_timestamp_marks():
    pages = FIXTURE["bybit"]["history"]

    async def handler(method, url, params):
        if url.endswith("open-interest"):
            return pages[1 if params.get("cursor") else 0]
        if url.endswith("mark-price-kline"):
            return FIXTURE["bybit"]["mark_history"]
        return FIXTURE["bybit"]["ticker"]

    transport = StubTransport(handler)
    result = asyncio.run(
        BybitAdapter(transport, lambda: 900.5).fetch(
            instrument("BYBIT"), HistoryRange(300_000, 900_000), include_history=True
        )
    )
    assert result.current.value_usd == 18
    assert [row.value_usd for row in result.history] == [6, 12, 18]
    assert all(row.valuation is ValuationMethod.MARK_PRICE for row in result.history)
    assert all(row.base_quantity is not None for row in result.history)
    history_requests = [
        params for _, url, params in transport.requests if url.endswith("open-interest")
    ]
    assert history_requests[1]["cursor"] == "next"


def test_linear_contract_history_reports_missing_exact_timestamp_marks():
    pages = FIXTURE["bybit"]["history"]
    partial_marks = json.loads(json.dumps(FIXTURE["bybit"]["mark_history"]))
    partial_marks["result"]["list"] = partial_marks["result"]["list"][:2]

    async def handler(method, url, params):
        if url.endswith("open-interest"):
            return pages[1 if params.get("cursor") else 0]
        if url.endswith("mark-price-kline"):
            return partial_marks
        return FIXTURE["bybit"]["ticker"]

    result = asyncio.run(
        BybitAdapter(StubTransport(handler), lambda: 900.5).fetch(
            instrument("BYBIT"),
            HistoryRange(300_000, 900_000),
            include_history=True,
        )
    )

    assert [point.timestamp_ms for point in result.history] == [600_000, 900_000]
    assert result.history_issue is not None
    assert result.history_issue.code == "history_partial"


def test_gate_current_includes_both_position_sides():
    async def handler(method, url, params):
        if url.endswith("contract_stats"):
            return FIXTURE["gate"]["history"]
        return FIXTURE["gate"]["details"]

    result = asyncio.run(
        GateAdapter(StubTransport(handler), lambda: 900.5).fetch(
            instrument("GATE"), None, include_history=True
        )
    )
    assert result.current.native_value == 10
    assert result.current.value_usd == 10
    assert result.current.base_quantity is not None
    assert float(result.current.base_quantity.amount) == 1
    assert result.history[0].value_usd == 8
    assert result.history[0].base_quantity is not None


def test_gate_continues_after_a_short_sparse_history_page(monkeypatch):
    pages = FIXTURE["gate"]["sparse_history"]
    monkeypatch.setattr(native, "GATE_HISTORY_LIMIT", 3)

    async def handler(method, url, params):
        if url.endswith("contract_stats"):
            return pages[0 if params["from"] == 300 else 1]
        return FIXTURE["gate"]["details"]

    transport = StubTransport(handler)
    result = asyncio.run(
        GateAdapter(transport, lambda: 1_500.5).fetch(
            instrument("GATE"), HistoryRange(300_000, 1_500_000), include_history=True
        )
    )

    assert [row.timestamp_ms for row in result.history] == [
        300_000,
        900_000,
        1_200_000,
        1_500_000,
    ]
    requests = [
        params
        for _, url, params in transport.requests
        if url.endswith("contract_stats")
    ]
    assert [row["from"] for row in requests] == [300, 901]


def test_gate_malformed_history_preserves_current_observation():
    async def handler(method, url, params):
        if url.endswith("contract_stats"):
            return FIXTURE["gate"]["malformed_history"]
        return FIXTURE["gate"]["details"]

    result = asyncio.run(
        GateAdapter(StubTransport(handler), lambda: 900.5).fetch(
            instrument("GATE"), HistoryRange(300_000, 900_000), include_history=True
        )
    )

    assert result.current.value_usd == 10
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"


def test_okx_preserves_reported_zero():
    async def handler(method, url, params):
        return FIXTURE["okx"]

    result = asyncio.run(
        OkxAdapter(StubTransport(handler)).fetch(
            instrument("OKX"), None, include_history=False
        )
    )
    assert result.current.value_usd == 0


def test_okx_dated_contract_uses_futures_identity_and_bounded_history():
    async def handler(method, url, params):
        if url.endswith("open-interest-history"):
            return {"code": "0", "data": FIXTURE["okx"]["history"]}
        return {"data": FIXTURE["okx"]["data"]}

    transport = StubTransport(handler)
    result = asyncio.run(
        OkxAdapter(transport).fetch(
            instrument("OKX", market_type="future"),
            HistoryRange(300_000, 600_000),
            include_history=True,
        )
    )

    assert [point.value_usd for point in result.history] == [5, 12]
    assert [float(point.base_quantity.amount) for point in result.history] == [2.5, 6]
    assert transport.requests[0][2]["instType"] == "FUTURES"


def test_bybit_inverse_open_interest_preserves_documented_quote_unit():
    async def handler(method, url, params):
        if url.endswith("open-interest"):
            return {"retCode": 0, "result": {"list": [{"timestamp": "600000", "openInterest": "12"}]}}
        return {"retCode": 0, "time": 700000, "result": {"list": [{"markPrice": "2", "openInterest": "18"}]}}

    result = asyncio.run(
        BybitAdapter(StubTransport(handler)).fetch(
            instrument("BYBIT", contract_direction=ContractDirection.INVERSE),
            HistoryRange(600_000, 600_000),
            include_history=True,
        )
    )

    assert result.current.native_unit is NativeUnit.QUOTE
    assert result.current.base_quantity is None
    assert result.current.value_usd == 18
    assert result.history[0].native_unit is NativeUnit.QUOTE


@pytest.mark.parametrize(
    "product", [None, "PERP"], ids=["no-product", "ordinary-product"]
)
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
    result = asyncio.run(adapter.fetch(subject, None, include_history=False))

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
    result = asyncio.run(
        HyperliquidAdapter(transport, lambda: 1).fetch(
            subject, None, include_history=False
        )
    )

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
def test_perpetual_universe_rejects_malformed_or_absent_observations(
    fixture_name, error
):
    payload = HYPERLIQUID_CONTEXTS[fixture_name]

    async def handler(method, url, params):
        return payload

    symbol = (
        payload[0]["universe"][0]["name"]
        if fixture_name == "malformed"
        else "ABSENT_NATIVE_SYMBOL"
    )
    with pytest.raises(error):
        asyncio.run(
            HyperliquidAdapter(StubTransport(handler), lambda: 1).fetch(
                instrument("HYPERLIQUID", symbol=symbol), None, include_history=False
            )
        )


def test_scoped_perpetual_identity_rejects_conflicting_native_scope():
    payload = HYPERLIQUID_CONTEXTS["scoped_success"]
    qualified_symbol = payload[0]["universe"][0]["name"]

    with pytest.raises(InvalidInstrument):
        asyncio.run(
            HyperliquidAdapter(StubTransport(None), lambda: 1).fetch(
                instrument(
                    "HYPERLIQUID",
                    symbol=qualified_symbol,
                    product="HIP-3:conflicting-scope",
                ),
                None,
                include_history=False,
            )
        )


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
        asyncio.run(
            HyperliquidAdapter(StubTransport(None), lambda: 1).fetch(
                instrument("HYPERLIQUID", symbol=local_symbol, product=product),
                None,
                include_history=False,
            )
        )


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
        asyncio.run(
            MexcAdapter(StubTransport(handler)).fetch(
                instrument("MEXC", symbol=symbol), None, include_history=False
            )
        )


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
        asyncio.run(
            MexcAdapter(StubTransport(handler)).fetch(
                instrument("MEXC", symbol=symbol), None, include_history=False
            )
        )


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
        results = await asyncio.gather(
            *(
                adapter.fetch(subject, None, include_history=False)
                for subject in subjects
            )
        )
        await transport.close()
        return results

    assert len(asyncio.run(scenario())) == 2
    assert calls == 1


def test_aggregate_base_unit_ticker_uses_response_time_and_exact_symbol():
    async def handler(method, url, params):
        return KRAKEN_OPEN_INTEREST["tickers"]

    selected = KRAKEN_OPEN_INTEREST["tickers"]["tickers"][1]
    transport = StubTransport(handler)
    adapter = KrakenAdapter(transport)
    subject = instrument(
        "KRAKEN",
        symbol=selected["symbol"],
        contract_multiplier=None,
    )
    result = asyncio.run(adapter.fetch(subject, None, include_history=False))

    assert result.current.timestamp_ms == 1_767_323_045_678
    assert result.current.native_value == float(selected["openInterest"])
    assert result.current.native_unit is NativeUnit.BASE
    assert result.current.mark_price == float(selected["markPrice"])
    assert result.current.value_usd == pytest.approx(50)
    assert result.current.valuation is ValuationMethod.MARK_PRICE
    assert result.current.timestamp_ms != selected["lastTime"]
    assert transport.requests == [
        ("GET", "https://futures.kraken.com/derivatives/api/v3/tickers", None)
    ]

    capabilities = adapter.capabilities(subject)
    assert capabilities.current is True
    assert capabilities.history is True
    assert capabilities.history_interval_seconds == 300
    assert capabilities.max_history_days == 6
    assert capabilities.required_metadata == ("contract_direction",)
    registered = OpenInterestClient(transport=transport).capabilities(subject)
    assert registered == capabilities


def test_aggregate_inverse_ticker_uses_quote_notional_multiplier():
    async def handler(method, url, params):
        return KRAKEN_OPEN_INTEREST["tickers"]

    selected = KRAKEN_OPEN_INTEREST["tickers"]["tickers"][2]
    subject = instrument(
        "KRAKEN",
        symbol=selected["symbol"],
        settlement_currency="BASE",
        contract_direction=ContractDirection.INVERSE,
        contract_multiplier=100,
    )
    adapter = KrakenAdapter(StubTransport(handler))
    result = asyncio.run(adapter.fetch(subject, None, include_history=False))

    assert result.current.native_value == 3
    assert result.current.native_unit is NativeUnit.CONTRACTS
    assert result.current.value_usd == 300
    assert result.current.mark_price == 200
    assert result.current.valuation is ValuationMethod.CONTRACT_VALUE
    assert adapter.capabilities(subject).required_metadata == (
        "contract_direction",
        "contract_multiplier",
    )


@pytest.mark.parametrize(
    "reported_mark",
    [None, "", "0", "-1"],
    ids=["missing", "empty", "zero", "negative"],
)
def test_aggregate_inverse_ticker_does_not_require_a_positive_mark(reported_mark):
    payload = json.loads(json.dumps(KRAKEN_OPEN_INTEREST["tickers"]))
    selected = payload["tickers"][2]
    if reported_mark is None:
        selected.pop("markPrice")
    else:
        selected["markPrice"] = reported_mark

    async def handler(method, url, params):
        return payload

    result = asyncio.run(
        KrakenAdapter(StubTransport(handler)).fetch(
            instrument(
                "KRAKEN",
                symbol=selected["symbol"],
                settlement_currency="BASE",
                contract_direction=ContractDirection.INVERSE,
                contract_multiplier=100,
            ),
            None,
            include_history=False,
        )
    )

    assert result.current.native_value == 3
    assert result.current.value_usd == 300
    assert result.current.mark_price is None
    assert result.current.valuation is ValuationMethod.CONTRACT_VALUE


@pytest.mark.parametrize(
    "direction",
    [None, "linear"],
    ids=["missing", "untyped"],
)
def test_aggregate_mixed_unit_ticker_requires_typed_contract_direction(direction):
    async def handler(method, url, params):
        return KRAKEN_OPEN_INTEREST["tickers"]

    with pytest.raises(InvalidInstrument):
        asyncio.run(
            KrakenAdapter(StubTransport(handler)).fetch(
                instrument(
                    "KRAKEN",
                    symbol="PF_LINEAR",
                    contract_direction=direction,
                ),
                None,
                include_history=False,
            )
        )


def test_aggregate_inverse_ticker_requires_contract_multiplier_before_request():
    transport = StubTransport(None)
    with pytest.raises(InvalidInstrument):
        asyncio.run(
            KrakenAdapter(transport).fetch(
                instrument(
                    "KRAKEN",
                    symbol="PI_INVERSE",
                    settlement_currency="BASE",
                    contract_direction=ContractDirection.INVERSE,
                    contract_multiplier=None,
                ),
                None,
                include_history=False,
            )
        )
    assert transport.requests == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(result="error"),
        lambda payload: payload.pop("tickers"),
        lambda payload: payload["tickers"].append({"symbol": None}),
        lambda payload: payload.pop("serverTime"),
        lambda payload: payload.update(serverTime="not-a-time"),
        lambda payload: payload["tickers"][1].pop("openInterest"),
        lambda payload: payload["tickers"][1].update(markPrice="0"),
    ],
    ids=[
        "rejected-envelope",
        "missing-rows",
        "malformed-row-identity",
        "missing-source-time",
        "malformed-source-time",
        "missing-open-interest",
        "non-positive-mark",
    ],
)
def test_aggregate_mixed_unit_ticker_rejects_invalid_payloads(mutate):
    payload = json.loads(json.dumps(KRAKEN_OPEN_INTEREST["tickers"]))
    mutate(payload)

    async def handler(method, url, params):
        return payload

    with pytest.raises(InvalidResponse):
        asyncio.run(
            KrakenAdapter(StubTransport(handler)).fetch(
                instrument(
                    "KRAKEN",
                    symbol="PF_LINEAR",
                    contract_multiplier=None,
                ),
                None,
                include_history=False,
            )
        )


@pytest.mark.parametrize("ambiguous", [False, True])
def test_aggregate_mixed_unit_ticker_requires_one_exact_instrument_match(ambiguous):
    payload = json.loads(json.dumps(KRAKEN_OPEN_INTEREST["tickers"]))
    symbol = "ABSENT_NATIVE_SYMBOL"
    if ambiguous:
        selected = payload["tickers"][1]
        payload["tickers"].append(dict(selected))
        symbol = selected["symbol"]

    async def handler(method, url, params):
        return payload

    with pytest.raises(DataUnavailable):
        asyncio.run(
            KrakenAdapter(StubTransport(handler)).fetch(
                instrument(
                    "KRAKEN",
                    symbol=symbol,
                    contract_multiplier=None,
                ),
                None,
                include_history=False,
            )
        )


@pytest.mark.parametrize("market_type", ["perpetual", "future"])
def test_base_unit_history_pages_forward_and_joins_exact_mark_timestamps(market_type):
    analytics = KRAKEN_OPEN_INTEREST["analytics_pages"]
    marks = KRAKEN_OPEN_INTEREST["mark_pages"]

    async def handler(method, url, params):
        if url.endswith("/open-interest"):
            return analytics[0 if params["since"] == 300 else 1]
        if "/mark/" in url:
            return marks[0 if params["from"] == 300 else 1]
        return KRAKEN_OPEN_INTEREST["tickers"]

    transport = StubTransport(handler)
    result = asyncio.run(
        KrakenAdapter(transport, lambda: 1_200.5).fetch(
            instrument(
                "KRAKEN",
                symbol="PF_LINEAR",
                contract_multiplier=None,
                market_type=market_type,
            ),
            HistoryRange(300_000, 900_000),
            include_history=True,
        )
    )

    assert [row.timestamp_ms for row in result.history] == [
        300_000,
        600_000,
        900_000,
    ]
    assert [row.native_value for row in result.history] == [1, 2, 3]
    assert [row.mark_price for row in result.history] == [2, 4, 6]
    assert [row.value_usd for row in result.history] == [2, 8, 18]
    assert all(row.native_unit is NativeUnit.BASE for row in result.history)
    assert all(row.valuation is ValuationMethod.MARK_PRICE for row in result.history)
    assert result.history_issue is None

    analytics_requests = [
        params
        for _, url, params in transport.requests
        if url.endswith("/open-interest")
    ]
    assert [row["since"] for row in analytics_requests] == [300, 900]
    assert all(row["to"] == 900 for row in analytics_requests)
    assert all(row["interval"] == 300 for row in analytics_requests)
    mark_requests = [params for _, url, params in transport.requests if "/mark/" in url]
    assert [row["from"] for row in mark_requests] == [300, 900]
    assert all(row["to"] == 900 for row in mark_requests)


@pytest.mark.parametrize("market_type", ["perpetual", "future"])
def test_contract_count_history_uses_multiplier_without_mark_requests(market_type):
    analytics = KRAKEN_OPEN_INTEREST["analytics_pages"]

    async def handler(method, url, params):
        if url.endswith("/open-interest"):
            return analytics[0 if params["since"] == 300 else 1]
        return KRAKEN_OPEN_INTEREST["tickers"]

    transport = StubTransport(handler)
    result = asyncio.run(
        KrakenAdapter(transport, lambda: 1_200.5).fetch(
            instrument(
                "KRAKEN",
                symbol="PI_INVERSE",
                settlement_currency="BASE",
                contract_direction=ContractDirection.INVERSE,
                contract_multiplier=100,
                market_type=market_type,
            ),
            HistoryRange(300_000, 900_000),
            include_history=True,
        )
    )

    assert [row.native_value for row in result.history] == [1, 2, 3]
    assert [row.value_usd for row in result.history] == [100, 200, 300]
    assert all(row.native_unit is NativeUnit.CONTRACTS for row in result.history)
    assert all(row.mark_price is None for row in result.history)
    assert all(
        row.valuation is ValuationMethod.CONTRACT_VALUE for row in result.history
    )
    assert not any("/mark/" in url for _, url, _ in transport.requests)


def test_base_unit_history_reports_missing_exact_mark_joins_as_partial():
    analytics = {
        **KRAKEN_OPEN_INTEREST["analytics_pages"][0],
        "result": {
            **KRAKEN_OPEN_INTEREST["analytics_pages"][0]["result"],
            "timestamp": [300, 600, 900],
            "data": [
                ["0.5", "1.25", "0.25", "1"],
                ["1", "2.5", "0.75", "2"],
                ["2", "3.5", "1.5", "3"],
            ],
            "more": False,
        },
    }

    async def handler(method, url, params):
        if url.endswith("/open-interest"):
            return analytics
        if "/mark/" in url:
            return KRAKEN_OPEN_INTEREST["partial_marks"]
        return KRAKEN_OPEN_INTEREST["tickers"]

    result = asyncio.run(
        KrakenAdapter(StubTransport(handler), lambda: 1_200.5).fetch(
            instrument("KRAKEN", symbol="PF_LINEAR", contract_multiplier=None),
            HistoryRange(300_000, 900_000),
            include_history=True,
        )
    )

    assert [row.timestamp_ms for row in result.history] == [300_000, 900_000]
    assert [row.value_usd for row in result.history] == [2, 18]
    assert result.history_issue is not None
    assert result.history_issue.code == "history_partial"
    assert "1 of 3" in result.history_issue.message


@pytest.mark.parametrize(
    "fixture_name",
    ["malformed_analytics", "misaligned_analytics"],
)
def test_malformed_analytics_history_preserves_current_observation(fixture_name):
    async def handler(method, url, params):
        if url.endswith("/open-interest"):
            return KRAKEN_OPEN_INTEREST[fixture_name]
        return KRAKEN_OPEN_INTEREST["tickers"]

    result = asyncio.run(
        KrakenAdapter(StubTransport(handler), lambda: 600.5).fetch(
            instrument("KRAKEN", symbol="PF_LINEAR", contract_multiplier=None),
            HistoryRange(300_000, 300_000),
            include_history=True,
        )
    )

    assert result.current.value_usd == 50
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"
    assert "InvalidResponse" in result.history_issue.message


def test_history_pagination_bound_preserves_current_observation(monkeypatch):
    monkeypatch.setattr(native, "HISTORY_MAX_PAGES", 1)

    async def handler(method, url, params):
        if url.endswith("/open-interest"):
            return KRAKEN_OPEN_INTEREST["analytics_pages"][0]
        return KRAKEN_OPEN_INTEREST["tickers"]

    result = asyncio.run(
        KrakenAdapter(StubTransport(handler), lambda: 1_200.5).fetch(
            instrument("KRAKEN", symbol="PF_LINEAR", contract_multiplier=None),
            HistoryRange(300_000, 900_000),
            include_history=True,
        )
    )

    assert result.current.value_usd == 50
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"
    assert "PaginationError" in result.history_issue.message


def test_history_lookback_is_clamped_below_native_page_ceiling():
    six_days_ms = 6 * 86_400_000
    clock_ms = 10 * six_days_ms
    current_bucket = clock_ms // 300_000 * 300_000
    latest_complete = current_bucket - 300_000
    expected_start = latest_complete - six_days_ms + 300_000

    async def handler(method, url, params):
        if url.endswith("/open-interest"):
            assert params == {
                "since": expected_start // 1000,
                "to": latest_complete // 1000,
                "interval": 300,
            }
            return {
                "result": {"timestamp": [], "data": [], "more": False},
                "errors": [],
            }
        if "/mark/" in url:
            assert params == {
                "from": expected_start // 1000,
                "to": latest_complete // 1000,
            }
            return {"candles": [], "more_candles": False}
        return KRAKEN_OPEN_INTEREST["tickers"]

    result = asyncio.run(
        KrakenAdapter(
            StubTransport(handler),
            lambda: clock_ms / 1000,
        ).fetch(
            instrument("KRAKEN", symbol="PF_LINEAR", contract_multiplier=None),
            HistoryRange(0, latest_complete),
            include_history=True,
        )
    )

    assert result.history == ()
    assert result.history_issue is None
    assert (latest_complete - expected_start) // 300_000 + 1 == 1_728


def test_client_requires_an_explicitly_available_adapter():
    client = OpenInterestClient(transport=StubTransport(None), adapters={})
    with pytest.raises(AdapterUnavailable):
        client.capabilities(instrument("UNKNOWN"))


def test_ccxt_symbol_resolution_requires_a_unique_contract():
    class Exchange:
        markets_by_id = {
            "BASEQUOTE": [{"symbol": "BASE/QUOTE:QUOTE", "contract": True}]
        }

    assert resolve_ccxt_symbol(Exchange(), instrument("VENUE")) == "BASE/QUOTE:QUOTE"


def test_ccxt_symbol_resolution_does_not_rewrite_provider_identity():
    class Exchange:
        markets_by_id = {
            "BASEQUOTE": [{"symbol": "BASE/QUOTE:QUOTE", "contract": True}]
        }

    with pytest.raises(DataUnavailable):
        resolve_ccxt_symbol(
            Exchange(),
            instrument("VENUE", symbol="basequote"),
        )
    assert (
        resolve_ccxt_symbol(
            Exchange(),
            instrument(
                "VENUE",
                symbol="EXACT-ENDPOINT-ID",
                pair_symbol="BASEQUOTE",
            ),
        )
        == "BASE/QUOTE:QUOTE"
    )


def test_specialized_aggregate_endpoint_uses_supplied_secondary_identity():
    row = [None] * 19
    row[1], row[15], row[18] = 1_700_000_000_000, "2", "3"

    async def handler(method, url, params):
        return [row]

    transport = StubTransport(handler)
    result = asyncio.run(
        BitfinexAdapter(transport).fetch(
            instrument(
                "BITFINEX",
                symbol="CATALOG-ID",
                pair_symbol="EXACT-ENDPOINT-ID",
            ),
            None,
            include_history=False,
        )
    )

    assert result.current.value_usd == 6
    assert transport.requests[0][2] == {"keys": "EXACT-ENDPOINT-ID"}


def test_derivative_status_history_uses_same_row_mark_and_exact_identity():
    async def handler(method, url, params):
        return (
            ADDED_VENUES["bitfinex"]["history"]
            if url.endswith("/hist")
            else ADDED_VENUES["bitfinex"]["current"]
        )

    result = asyncio.run(
        BitfinexAdapter(StubTransport(handler)).fetch(
            instrument(
                "BITFINEX",
                pair_symbol="tBASEF0:QUOTEF0",
                contract_multiplier=0.01,
            ),
            HistoryRange(1_699_999_800_000, 1_700_000_100_000),
            include_history=True,
        )
    )

    assert result.current.value_usd == 20
    assert result.history[0].timestamp_ms == 1_699_999_800_000
    assert result.history[0].mark_price == 19
    assert result.history[0].value_usd == pytest.approx(17.1)


def test_dated_delivery_current_uses_the_exact_delivery_route_without_funding_or_history():
    async def handler(method, url, params):
        assert "/delivery/" in url
        return FIXTURE["gate"]["details"]

    result = asyncio.run(
        GateAdapter(StubTransport(handler), lambda: 900.5).fetch(
            instrument("GATE", market_type="future"),
            HistoryRange(300_000, 900_000),
            include_history=True,
        )
    )

    assert result.current.value_usd == 10
    assert result.history == ()


def test_malformed_derivative_status_history_preserves_current_snapshot():
    async def handler(method, url, params):
        return (
            ADDED_VENUES["bitfinex"]["malformed_history"]
            if url.endswith("/hist")
            else ADDED_VENUES["bitfinex"]["current"]
        )

    result = asyncio.run(
        BitfinexAdapter(StubTransport(handler)).fetch(
            instrument("BITFINEX", pair_symbol="tBASEF0:QUOTEF0"),
            None,
            include_history=True,
        )
    )

    assert result.current.value_usd == 2_000
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"


@pytest.mark.parametrize(
    ("adapter", "venue", "symbol", "expected"),
    [
        (DeepcoinAdapter, "DEEPCOIN", "BASE-QUOTE-SWAP", 20),
        (KucoinAdapter, "KUCOIN", "BASEQUOTEM", 20),
        (HtxAdapter, "HTX", "BASE-QUOTE", 30),
    ],
)
def test_added_native_current_open_interest_preserves_proven_quantities(
    adapter, venue, symbol, expected
):
    async def handler(method, url, params):
        fixture = ADDED_VENUES[venue.lower()]
        if venue == "DEEPCOIN":
            return fixture["current_mark"] if url.endswith("mark-price") else fixture["current_oi"]
        if venue == "KUCOIN" and url.endswith("open-interest"):
            return fixture["current_oi"]
        return fixture["current"]

    result = asyncio.run(
        adapter(StubTransport(handler), lambda: 1_700_000_100).fetch(
            instrument(venue, symbol=symbol, contract_multiplier=0.01),
            None,
            include_history=False,
        )
    )

    assert result.current.value_usd == expected
    assert result.current.base_quantity is not None


@pytest.mark.parametrize(
    ("adapter", "venue", "symbol"),
    [
        (DeepcoinAdapter, "DEEPCOIN", "BASE-QUOTE-SWAP"),
        (KucoinAdapter, "KUCOIN", "BASEQUOTEM"),
    ],
)
def test_exact_mark_history_join_reports_missing_buckets_without_losing_current(
    adapter, venue, symbol
):
    fixture = ADDED_VENUES[venue.lower()]

    async def handler(method, url, params):
        if venue == "DEEPCOIN":
            if url.endswith("mark-price"):
                return fixture["current_mark"]
            if url.endswith("mark-price-candles"):
                return fixture["history_marks_partial"]
            if params.get("limit") == 1:
                return fixture["current_oi"]
            return fixture["history_oi"]
        if url.endswith(f"contracts/{symbol}"):
            return fixture["current"]
        if url.endswith("open-interest"):
            return fixture["history"] if params.get("interval") else fixture["current_oi"]
        return {"code":"200000", "data":{"list":fixture["marks_partial"]["data"]}}

    result = asyncio.run(
        adapter(StubTransport(handler), lambda: 1_700_000_100).fetch(
            instrument(venue, symbol=symbol, contract_multiplier=0.01),
            HistoryRange(1_699_999_800_000, 1_700_000_100_000),
            include_history=True,
        )
    )

    assert len(result.history) == 1
    assert result.history_issue is not None
    assert result.history_issue.code == "history_partial"


@pytest.mark.parametrize(
    ("adapter", "venue", "symbol"),
    [
        (DeepcoinAdapter, "DEEPCOIN", "BASE-QUOTE-SWAP"),
        (KucoinAdapter, "KUCOIN", "BASEQUOTEM"),
        (HtxAdapter, "HTX", "BASE-QUOTE"),
    ],
)
def test_added_native_malformed_current_payload_is_rejected(adapter, venue, symbol):
    async def handler(method, url, params):
        return ADDED_VENUES[venue.lower()]["malformed"]

    with pytest.raises((InvalidResponse, DataUnavailable)):
        asyncio.run(
            adapter(StubTransport(handler)).fetch(
                instrument(venue, symbol=symbol), None, include_history=False
            )
        )


def test_history_failure_preserves_valid_htx_current_snapshot():
    async def handler(method, url, params):
        return (
            ADDED_VENUES["htx"]["history_missing"]
            if url.endswith("swap_his_open_interest")
            else ADDED_VENUES["htx"]["current"]
        )

    result = asyncio.run(
        HtxAdapter(StubTransport(handler)).fetch(
            instrument("HTX", symbol="BASE-QUOTE"), None, include_history=True
        )
    )

    assert result.current.value_usd == 30
    assert result.history == ()
    assert result.history_issue is not None


@pytest.mark.parametrize(
    ("adapter", "venue", "symbol", "fixture_key"),
    [
        (ToobitAdapter, "TOOBIT", "BASE-SWAP-QUOTE", "toobit"),
        (PhemexAdapter, "PHEMEX", "BASEQUOTE", "phemex"),
        (GrvtAdapter, "GRVT", "BASE_QUOTE_Perp", "grvt"),
        (LighterAdapter, "LIGHTER", "7", "lighter"),
    ],
)
def test_ranked_native_base_open_interest_uses_exact_identity_and_current_mark(
    adapter, venue, symbol, fixture_key
):
    fixture = ADDED_VENUES[fixture_key]

    async def handler(method, url, params):
        if venue == "TOOBIT":
            return fixture["current_mark"] if url.endswith("markPrice") else fixture["current_oi"]
        if venue == "PHEMEX":
            return fixture["linear"]
        return fixture["current"]

    transport = StubTransport(handler)
    result = asyncio.run(
        adapter(transport, lambda: 1_700_000_100).fetch(
            instrument(venue, symbol=symbol), None, include_history=True
        )
    )

    assert result.current.value_usd == 20
    assert result.current.native_unit is NativeUnit.BASE
    assert result.current.base_quantity is not None
    assert result.history == ()
    if venue in {"TOOBIT", "LIGHTER"}:
        assert result.current.timestamp_kind is ObservationTimeKind.RETRIEVED


def test_phemex_inverse_open_interest_preserves_contract_count_and_quote_value():
    async def handler(method, url, params):
        assert "/md/v1/" in url
        return ADDED_VENUES["phemex"]["inverse"]

    result = asyncio.run(
        PhemexAdapter(StubTransport(handler)).fetch(
            instrument(
                "PHEMEX",
                contract_direction=ContractDirection.INVERSE,
                contract_multiplier=10,
            ),
            None,
            include_history=False,
        )
    )

    assert result.current.value_usd == 40
    assert result.current.native_value == 4
    assert result.current.native_unit is NativeUnit.CONTRACTS
    assert result.current.mark_price is None


def test_provider_reported_notional_preserves_source_time_without_inventing_quantity():
    fixture = ADDED_VENUES["xt"]

    async def handler(method, url, params):
        assert "fapi.xt.com" in url
        assert params == {"symbol": "BASE_QUOTE"}
        return fixture["current"]

    result = asyncio.run(
        XtAdapter(StubTransport(handler)).fetch(
            instrument("XT", symbol="BASE_QUOTE"), None, include_history=True
        )
    )

    assert result.current.value_usd == 20
    assert result.current.native_value is None
    assert result.current.native_unit is None
    assert result.current.timestamp_kind is ObservationTimeKind.SOURCE
    assert result.history == ()


def test_provider_reported_notional_rejects_mismatched_identity():
    async def handler(method, url, params):
        return ADDED_VENUES["xt"]["malformed"]

    with pytest.raises(InvalidResponse):
        asyncio.run(
            XtAdapter(StubTransport(handler)).fetch(
                instrument("XT", symbol="BASE_QUOTE"),
                None,
                include_history=False,
            )
        )


def test_documentation_insufficient_open_interest_is_not_exposed_by_optional_runtime():
    subject = instrument("WHITEBIT")
    adapter = CcxtAdapter()

    assert adapter.capabilities(subject).current is False
    assert adapter.capabilities(subject).history is False
    with pytest.raises(DataUnavailable, match="not supported"):
        asyncio.run(adapter.fetch(subject, None, include_history=False))


@pytest.mark.parametrize(
    ("adapter", "venue", "symbol", "fixture_key"),
    [
        (ToobitAdapter, "TOOBIT", "BASE-SWAP-QUOTE", "toobit"),
        (PhemexAdapter, "PHEMEX", "BASEQUOTE", "phemex"),
        (GrvtAdapter, "GRVT", "BASE_QUOTE_Perp", "grvt"),
        (LighterAdapter, "LIGHTER", "7", "lighter"),
    ],
)
def test_ranked_native_current_open_interest_rejects_malformed_payload(
    adapter, venue, symbol, fixture_key
):
    fixture = ADDED_VENUES[fixture_key]

    async def handler(method, url, params):
        if venue == "TOOBIT" and url.endswith("markPrice"):
            return fixture["current_mark"]
        return fixture["malformed"]

    with pytest.raises((InvalidResponse, DataUnavailable)):
        asyncio.run(
            adapter(StubTransport(handler)).fetch(
                instrument(venue, symbol=symbol), None, include_history=False
            )
        )


def test_grvt_rejects_non_futures_product_identity_before_request():
    async def handler(method, url, params):
        raise AssertionError("unsupported product must not be requested")

    with pytest.raises(InvalidInstrument):
        asyncio.run(
            GrvtAdapter(StubTransport(handler)).fetch(
                instrument("GRVT", market_type="option"),
                None,
                include_history=False,
            )
        )


def test_btse_current_contract_count_joins_exact_mark_identity():
    async def handler(method, url, params):
        return (
            ADDED_VENUES["btse"]["mark"]
            if url.endswith("indices")
            else ADDED_VENUES["btse"]["ticker"]
        )

    result = asyncio.run(
        BtseAdapter(StubTransport(handler)).fetch(
            instrument(
                "BTSE",
                symbol="BASE-PERP-QUOTE",
                contract_multiplier=0.01,
            ),
            None,
            include_history=True,
        )
    )

    assert result.current.native_value == 400
    assert result.current.native_unit is NativeUnit.CONTRACTS
    assert result.current.value_usd == 20
    assert result.current.timestamp_kind is ObservationTimeKind.SOURCE
    assert result.history == ()


def test_kucoin_dated_inverse_contract_uses_current_and_bounded_history_protocols():
    fixture = ADDED_VENUES["kucoin"]

    async def handler(method, url, params):
        if "/contracts/" in url:
            return fixture["current"]
        if url.endswith("open-interest"):
            return fixture["history"] if params.get("interval") else fixture["current_oi"]
        return {"code": "200000", "data": {"list": fixture["marks_partial"]["data"]}}

    result = asyncio.run(
        KucoinAdapter(StubTransport(handler)).fetch(
            instrument(
                "KUCOIN",
                symbol="BASEQUOTEM",
                market_type="future",
                contract_direction=ContractDirection.INVERSE,
                contract_multiplier=10,
            ),
            HistoryRange(1_699_999_800_000, 1_700_000_100_000),
            include_history=True,
        )
    )

    assert result.current.value_usd == 2_000
    assert [point.value_usd for point in result.history] == [1_900, 2_000]
    assert result.history_issue is None


def test_htx_dated_linear_contract_preserves_reported_notional_and_history():
    fixture = ADDED_VENUES["htx"]

    async def handler(method, url, params):
        assert params["business_type"] == "futures"
        return (
            fixture["dated_linear_history"]
            if url.endswith("swap_his_open_interest")
            else fixture["dated_linear_current"]
        )

    result = asyncio.run(
        HtxAdapter(StubTransport(handler)).fetch(
            instrument(
                "HTX",
                symbol="BASE-QUOTE-260101",
                market_type="future",
                contract_multiplier=0.01,
            ),
            None,
            include_history=True,
        )
    )

    assert result.current.value_usd == 30
    assert result.history[0].value_usd == 29
    assert float(result.history[0].base_quantity.amount) == 2.9


def test_htx_dated_inverse_contract_reuses_current_identity_for_hourly_history():
    async def handler(method, url, params):
        if url.endswith("contract_his_open_interest"):
            assert params["symbol"] == "BASE"
            assert params["contract_type"] == "quarter"
            assert params["period"] == "60min"
            return ADDED_VENUES["htx"]["dated_inverse_history"]
        return ADDED_VENUES["htx"]["dated_inverse_current"]

    result = asyncio.run(
        HtxAdapter(StubTransport(handler)).fetch(
            instrument(
                "HTX",
                symbol="BASE260101",
                market_type="future",
                contract_direction=ContractDirection.INVERSE,
                contract_multiplier=10,
            ),
            None,
            include_history=True,
        )
    )

    assert result.current.value_usd == 3_000
    assert result.history[0].value_usd == 2_900
    assert result.history_issue is None


def test_inverse_history_requires_explicit_pair_identity_without_rewriting_symbol():
    async def handler(method, url, params):
        if url.endswith("openInterest"):
            return FIXTURE["binance"]["current"]
        if url.endswith("premiumIndex"):
            return FIXTURE["binance"]["mark"]
        raise AssertionError("history must not be requested without pair identity")

    result = asyncio.run(
        BinanceAdapter(StubTransport(handler), lambda: 900.5).fetch(
            instrument(
                "BINANCE",
                symbol="NATIVE-PERP-ID",
                contract_direction=ContractDirection.INVERSE,
                pair_symbol=None,
            ),
            None,
            include_history=True,
        )
    )

    assert result.current.value_usd == 10
    assert result.history == ()
    assert result.history_issue is not None
    assert "pair_symbol" in result.history_issue.message


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


def test_ranked_optional_metrics_use_exact_runtime_provider_ids():
    adapter = CcxtAdapter()

    assert {
        venue: adapter.exchange_ids[venue]
        for venue in (
            "WEEX", "BINGX", "ASTER", "DIGIFINEX", "CRYPTOCOM", "BLOFIN"
        )
    } == {
        "WEEX": "weex",
        "BINGX": "bingx",
        "ASTER": "aster",
        "DIGIFINEX": "digifinex",
        "CRYPTOCOM": "cryptocom",
        "BLOFIN": "blofin",
    }


def test_ccxt_hourly_history_uses_supported_cadence_and_source_timestamps(monkeypatch):
    exchange = StubCcxtExchange(CCXT_HOURLY["history"])
    adapter = configured_ccxt_adapter(monkeypatch, exchange)
    first, last = CCXT_HOURLY["history"]
    requested = HistoryRange(first["timestamp"], last["timestamp"])

    result = asyncio.run(
        adapter.fetch(
            instrument("HTX", symbol=CCXT_HOURLY["market_id"]),
            requested,
            include_history=True,
        )
    )

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
    assert all(
        row.valuation is ValuationMethod.VENUE_REPORTED for row in result.history
    )
    assert exchange.history_requests == [
        {
            "symbol": CCXT_HOURLY["symbol"],
            "timeframe": "1h",
            "since": first["timestamp"],
            "limit": 200,
        }
    ]


def test_ccxt_malformed_hourly_history_preserves_current(monkeypatch):
    exchange = StubCcxtExchange(CCXT_HOURLY["malformed_history"])
    adapter = configured_ccxt_adapter(monkeypatch, exchange)

    result = asyncio.run(
        adapter.fetch(
            instrument("HTX", symbol=CCXT_HOURLY["market_id"]),
            None,
            include_history=True,
        )
    )

    assert result.current.value_usd == CCXT_HOURLY["current"]["openInterestValue"]
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"
    assert "InvalidResponse" in result.history_issue.message


def test_ccxt_runtime_without_hourly_history_preserves_current(monkeypatch):
    exchange = StubCcxtExchange([], supports_history=False)
    adapter = configured_ccxt_adapter(monkeypatch, exchange)

    result = asyncio.run(
        adapter.fetch(
            instrument("HTX", symbol=CCXT_HOURLY["market_id"]),
            None,
            include_history=True,
        )
    )

    assert result.current.value_usd == CCXT_HOURLY["current"]["openInterestValue"]
    assert result.history == ()
    assert result.history_issue is not None
    assert result.history_issue.code == "history_unavailable"
    assert exchange.history_requests == []
