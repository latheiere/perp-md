from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlsplit

import httpx

from perp_md.errors import PerpMdError, RequestError


class JsonTransport(Protocol):
    async def get(self, url: str, params: dict[str, Any] | None = None) -> Any: ...
    async def post(self, url: str, payload: dict[str, Any]) -> Any: ...
    async def close(self) -> None: ...


@dataclass
class HttpxTransport:
    timeout_seconds: float = 10
    request_concurrency: int = 16
    per_host_concurrency: int = 4
    cache_ttl_seconds: float = 3
    _http: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _global: asyncio.Semaphore = field(init=False, repr=False)
    _hosts: dict[str, asyncio.Semaphore] = field(default_factory=dict, init=False, repr=False)
    _cache: dict[str, tuple[float, asyncio.Task[Any]]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.request_concurrency <= 0 or self.per_host_concurrency <= 0:
            raise ValueError("concurrency limits must be positive")
        self._global = asyncio.Semaphore(self.request_concurrency)

    async def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        key = f"GET:{url}:{json.dumps(params or {}, sort_keys=True, separators=(',', ':'))}"

        async def request() -> Any:
            client = await self._client()
            host = urlsplit(url).hostname or "unknown"
            async with self._global, self._hosts.setdefault(
                host, asyncio.Semaphore(self.per_host_concurrency)
            ):
                response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

        return await self._cached(key, request)

    async def post(self, url: str, payload: dict[str, Any]) -> Any:
        key = f"POST:{url}:{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"

        async def request() -> Any:
            client = await self._client()
            host = urlsplit(url).hostname or "unknown"
            async with self._global, self._hosts.setdefault(
                host, asyncio.Semaphore(self.per_host_concurrency)
            ):
                response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()

        return await self._cached(key, request)

    async def close(self) -> None:
        tasks = [task for _, task in self._cache.values() if not task.done()]
        self._cache.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._http is not None:
            client, self._http = self._http, None
            await client.aclose()

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                limits=httpx.Limits(
                    max_connections=self.request_concurrency,
                    max_keepalive_connections=min(8, self.request_concurrency),
                ),
                follow_redirects=False,
            )
        return self._http

    async def _cached(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] <= self.cache_ttl_seconds:
            return await asyncio.shield(cached[1])
        task = asyncio.create_task(factory())
        self._cache[key] = now, task
        try:
            return await asyncio.shield(task)
        except PerpMdError:
            self._cache.pop(key, None)
            raise
        except (httpx.HTTPError, ValueError) as exc:
            self._cache.pop(key, None)
            raise RequestError("venue request failed") from exc
