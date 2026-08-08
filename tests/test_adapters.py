from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from perp_md import (
    AdapterUnavailable,
    ContractDirection,
    DataUnavailable,
    HistoryRange,
    Instrument,
    InvalidResponse,
    NativeUnit,
    OpenInterestClient,
    ValuationMethod,
)
from perp_md.adapters.ccxt import resolve_ccxt_symbol
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


def test_hyperliquid_resolves_exact_native_symbol():
    async def handler(method, url, params):
        return FIXTURE["hyperliquid"]

    result = asyncio.run(HyperliquidAdapter(StubTransport(handler), lambda: 1).fetch(
        instrument("HYPERLIQUID", symbol="BASE-PERP"), None, include_history=False
    ))
    assert result.current.value_usd == 8


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
