from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from perp_md import (
    AdapterUnavailable,
    ContractDirection,
    HistoryRange,
    Instrument,
    OpenInterestClient,
    ValuationMethod,
)
from perp_md.adapters.ccxt import resolve_ccxt_symbol
from perp_md.adapters.native import (
    BinanceAdapter,
    BybitAdapter,
    GateAdapter,
    HyperliquidAdapter,
    OkxAdapter,
)
import perp_md.adapters.native as native


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "native_open_interest.json").read_text()
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


def test_client_requires_an_explicitly_available_adapter():
    client = OpenInterestClient(transport=StubTransport(None), adapters={})
    with pytest.raises(AdapterUnavailable):
        client.capabilities(instrument("UNKNOWN"))


def test_ccxt_symbol_resolution_requires_a_unique_contract():
    class Exchange:
        markets_by_id = {"BASEQUOTE": [{"symbol": "BASE/QUOTE:QUOTE", "contract": True}]}

    assert resolve_ccxt_symbol(Exchange(), instrument("VENUE")) == "BASE/QUOTE:QUOTE"
