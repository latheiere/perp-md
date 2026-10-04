from __future__ import annotations

import asyncio
import gc
import weakref

import httpx
import pytest
from perp_md.errors import RequestError

from perp_md.transport import HttpxTransport


def test_unique_request_payloads_expire_without_another_request():
    class Payload(dict):
        pass

    async def scenario():
        transport = HttpxTransport(cache_ttl_seconds=0.01)
        references = []

        async def request():
            payload = Payload(body="x" * 32768)
            references.append(weakref.ref(payload))
            return payload

        try:
            for index in range(128):
                await transport._cached(f"history-window-{index}", request)
            await asyncio.sleep(0.05)
            gc.collect()
            assert not transport._cache
            assert not transport._expiry
            assert all(reference() is None for reference in references)
        finally:
            await transport.close()

    asyncio.run(scenario())


def test_pending_request_remains_shared_after_the_response_ttl():
    async def scenario():
        transport = HttpxTransport(cache_ttl_seconds=0.01)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def request():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {"ok": True}

        first = asyncio.create_task(transport._cached("snapshot", request))
        await entered.wait()
        await asyncio.sleep(0.02)
        second = asyncio.create_task(transport._cached("snapshot", request))
        await asyncio.sleep(0)
        release.set()
        try:
            values = await asyncio.gather(first, second)
            assert values[0] is values[1]
            assert calls == 1
        finally:
            await transport.close()
        assert not transport._expiry

    asyncio.run(scenario())


def test_http_transport_deduplicates_identical_concurrent_requests(monkeypatch):
    calls = 0

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class Client:
        async def get(self, url, params=None):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return Response()

        async def aclose(self):
            return None

    transport = HttpxTransport()
    transport._http = Client()

    async def scenario():
        values = await asyncio.gather(
            transport.get("https://data.invalid/public"),
            transport.get("https://data.invalid/public"),
        )
        await transport.close()
        return values

    assert asyncio.run(scenario()) == [{"ok": True}, {"ok": True}]
    assert calls == 1


def test_http_transport_observes_failure_after_shielded_waiter_is_cancelled():
    reported: list[dict[str, object]] = []

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        release = asyncio.Event()
        started = asyncio.Event()
        transport = HttpxTransport()

        async def request():
            started.set()
            await release.wait()
            raise RuntimeError("transient request failure")

        waiter = asyncio.create_task(transport._cached("request", request))
        await started.wait()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await transport.close()

    asyncio.run(scenario())

    assert reported == []


@pytest.mark.parametrize("method", ["get", "post"])
def test_shared_request_failures_are_retryable_for_every_waiter(method):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
                raise httpx.ConnectTimeout("connection timed out", request=request)
            return httpx.Response(200, json={"ok": True})

        transport = HttpxTransport()
        transport._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async def fetch():
            if method == "get":
                return await transport.get("https://data.invalid/snapshot")
            return await transport.post("https://data.invalid/snapshot", {})

        tasks = [asyncio.create_task(fetch()) for _ in range(4)]
        await started.wait()
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, RequestError) for result in results)
        assert all(isinstance(result.__cause__, httpx.ConnectTimeout) for result in results)
        assert await fetch() == {"ok": True}
        assert calls == 2
        await transport.close()

    asyncio.run(scenario())


def test_rejected_response_invalidation_preserves_newer_cached_response():
    async def scenario():
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"generation": calls})
        transport = HttpxTransport()
        transport._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        url = "https://data.invalid/snapshot"
        first = await transport.get(url)
        transport.invalidate_get(url, response=first)
        second = await transport.get(url)
        transport.invalidate_get(url, response=first)
        assert await transport.get(url) is second
        assert calls == 2
        await transport.close()
    asyncio.run(scenario())
