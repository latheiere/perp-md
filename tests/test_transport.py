from __future__ import annotations

import asyncio

from perp_md.transport import HttpxTransport


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
