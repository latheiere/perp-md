import asyncio
from types import SimpleNamespace

import pytest

import perp_md.adapters.ccxt as module
from perp_md import DataUnavailable, Instrument, RequestError
from perp_md.adapters.ccxt import CcxtAdapter, _catalog_bytes
from perp_md.adapters.ccxt_funding import CcxtFundingAdapter


def instrument(symbol="BASEQUOTE", venue="XT"):
    return Instrument(venue=venue, symbol=symbol, base_currency="BASE", quote_currency="QUOTE")


class Exchange:
    has = {"fetchOpenInterest": True, "fetchFundingRate": True}

    def __init__(self, identities, *, load_error=None):
        self.markets_by_id = {
            identity: [{"symbol": identity + "/QUOTE:QUOTE", "contract": True}]
            for identity in identities
        }
        self.closed = False
        self.load_error = load_error
        self.release = None
        self.started = asyncio.Event()

    async def load_markets(self):
        await asyncio.sleep(0)
        if self.load_error is not None:
            raise self.load_error

    async def fetch_open_interest(self, symbol):
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        assert not self.closed
        return {"openInterestValue": 125, "timestamp": 1_700_000_000_000}

    async def fetch_funding_rate(self, symbol):
        await self.fetch_open_interest(symbol)
        return {"fundingRate": 0.0001, "timestamp": 1_700_000_000_000, "interval": "1h"}

    async def close(self):
        self.closed = True


def runtime(monkeypatch, snapshots):
    created = []

    def create(_config):
        result = snapshots[len(created)]
        created.append(result)
        return result

    monkeypatch.setattr(module.importlib, "import_module", lambda _: SimpleNamespace(xt=create))
    return created


async def fetch(adapter, symbol="BASEQUOTE", venue="XT"):
    return await adapter.fetch(instrument(symbol, venue), None, include_history=False)


@pytest.mark.parametrize("adapter_type", [CcxtAdapter, CcxtFundingAdapter])
def test_expiry_releases_idle_catalog_without_another_request(monkeypatch, adapter_type):
    exchange = Exchange(["BASEQUOTE"])
    runtime(monkeypatch, [exchange])

    async def run():
        adapter = adapter_type(catalog_ttl_seconds=0.02)
        await fetch(adapter)
        await asyncio.sleep(0.05)
        assert not adapter.exchanges
        assert not adapter._catalog_sizes
        assert not adapter._loaded_at
        assert not adapter._users
        assert not adapter._expiry
        assert exchange.closed
        await adapter.close()

    asyncio.run(run())


@pytest.mark.parametrize("adapter_type", [CcxtAdapter, CcxtFundingAdapter])
def test_concurrent_new_identities_refresh_one_aged_catalog(monkeypatch, adapter_type):
    old = Exchange(["BASEQUOTE"])
    new = Exchange(["BASEQUOTE", "NEWQUOTE", "OTHERQUOTE"])
    created = runtime(monkeypatch, [old, new])

    async def run():
        adapter = adapter_type()
        await fetch(adapter)
        adapter._loaded_at["XT"] -= 61
        results = await asyncio.gather(fetch(adapter, "NEWQUOTE"), fetch(adapter, "OTHERQUOTE"))
        assert all(result.current is not None for result in results)
        assert created == [old, new]
        await adapter.close()
        assert old.closed and new.closed

    asyncio.run(run())


def test_missing_or_ambiguous_identity_cannot_trigger_unbounded_refreshes(monkeypatch):
    old = Exchange(["BASEQUOTE"])
    new = Exchange(["BASEQUOTE"])
    new.markets_by_id["AMBIGUOUS"] = [
        {"symbol": "FIRST/QUOTE:QUOTE", "contract": True},
        {"symbol": "SECOND/QUOTE:QUOTE", "contract": True},
    ]
    created = runtime(monkeypatch, [old, new])

    async def run():
        adapter = CcxtAdapter()
        await fetch(adapter)
        with pytest.raises(DataUnavailable):
            await fetch(adapter, "MISSING")
        assert created == [old]
        adapter._loaded_at["XT"] -= 61
        for symbol in ("MISSING", "AMBIGUOUS", "MISSING"):
            with pytest.raises(DataUnavailable):
                await fetch(adapter, symbol)
        assert created == [old, new]
        await adapter.close()

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_expiry_preserves_inflight_request_and_closes_after_release(monkeypatch, cancel):
    old = Exchange(["BASEQUOTE"])
    old.release = asyncio.Event()
    new = Exchange(["BASEQUOTE"])
    runtime(monkeypatch, [old, new])

    async def run():
        adapter = CcxtAdapter(catalog_ttl_seconds=0.02)
        task = asyncio.create_task(fetch(adapter))
        await old.started.wait()
        await asyncio.sleep(0.04)
        assert not old.closed
        assert not adapter.exchanges
        await fetch(adapter)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            old.release.set()
            assert (await task).current.value_usd == 125
        await adapter.close()
        assert old.closed and new.closed
        assert not adapter._retired and not adapter._users

    asyncio.run(run())


def test_failed_refresh_closes_replacement_and_preserves_request_failure(monkeypatch):
    old = Exchange(["BASEQUOTE"])
    failed = Exchange([], load_error=ValueError("malformed catalog"))
    runtime(monkeypatch, [old, failed])

    async def run():
        adapter = CcxtAdapter()
        await fetch(adapter)
        adapter._loaded_at["XT"] -= 61
        with pytest.raises(RequestError, match="venue market catalog failed"):
            await fetch(adapter, "NEWQUOTE")
        with pytest.raises(RequestError, match="cooling down"):
            await fetch(adapter, "NEWQUOTE")
        await adapter.close()
        assert old.closed and failed.closed
        assert not adapter.exchanges and not adapter._users
        assert not adapter._retry_after and not adapter._retry_expiry

    asyncio.run(run())


def test_byte_and_entry_limits_evict_idle_catalogs(monkeypatch):
    exchanges = [Exchange(["BASEQUOTE"]) for _ in range(3)]
    runtime(monkeypatch, exchanges)

    async def run():
        adapter = CcxtAdapter(
            exchange_ids={venue: "xt" for venue in ("FIRST", "SECOND", "THIRD")},
            catalog_max_entries=2,
            catalog_max_bytes=_catalog_bytes(exchanges[0]) * 2,
        )
        for venue in ("FIRST", "SECOND", "THIRD"):
            await fetch(adapter, venue=venue)
            assert len(adapter.exchanges) <= 2
            assert sum(adapter._catalog_sizes.values()) <= adapter.catalog_max_bytes
        assert "FIRST" not in adapter.exchanges
        await adapter.close()
        assert all(exchange.closed for exchange in exchanges)

    asyncio.run(run())


@pytest.mark.parametrize("adapter_type", [CcxtAdapter, CcxtFundingAdapter])
def test_oversized_catalog_serves_request_without_retention(monkeypatch, adapter_type):
    exchange = Exchange(["BASEQUOTE"])
    runtime(monkeypatch, [exchange])

    async def run():
        adapter = adapter_type(catalog_max_bytes=1)
        assert (await fetch(adapter)).current is not None
        await asyncio.sleep(0)
        await adapter.close()
        assert exchange.closed
        assert not adapter.exchanges and not adapter._users

    asyncio.run(run())


def test_shared_catalog_aliases_are_not_counted_as_duplicate_payloads():
    exchange = Exchange(["BASEQUOTE"])
    size = _catalog_bytes(exchange)
    exchange.markets = exchange.markets_by_id
    assert _catalog_bytes(exchange) == size
