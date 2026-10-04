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
    _hosts: dict[str, asyncio.Semaphore] = field(
        default_factory=dict, init=False, repr=False
    )
    _cache: dict[str, tuple[float, asyncio.Task[Any]]] = field(
        default_factory=dict, init=False, repr=False
    )
    _expiry: dict[str, asyncio.TimerHandle] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.request_concurrency <= 0 or self.per_host_concurrency <= 0:
            raise ValueError("concurrency limits must be positive")
        if self.cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds must not be negative")
        self._global = asyncio.Semaphore(self.request_concurrency)

    async def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        key = f"GET:{url}:{json.dumps(params or {}, sort_keys=True, separators=(',', ':'))}"

        async def request() -> Any:
            client = await self._client()
            host = urlsplit(url).hostname or "unknown"
            async with (
                self._global,
                self._hosts.setdefault(
                    host, asyncio.Semaphore(self.per_host_concurrency)
                ),
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
            async with (
                self._global,
                self._hosts.setdefault(
                    host, asyncio.Semaphore(self.per_host_concurrency)
                ),
            ):
                response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()

        return await self._cached(key, request)

    async def close(self) -> None:
        tasks = [task for _, task in self._cache.values() if not task.done()]
        for handle in self._expiry.values():
            handle.cancel()
        self._expiry.clear()
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
        if cached and (not cached[1].done() or now - cached[0] <= self.cache_ttl_seconds):
            task = cached[1]
        else:
            if cached:
                self._evict(key, cached[1])
            task = asyncio.create_task(factory())
            task.add_done_callback(lambda completed: self._completed(key, completed))
            self._cache[key] = now, task
        try:
            return await asyncio.shield(task)
        except PerpMdError:
            self._evict(key, task)
            raise
        except (httpx.HTTPError, ValueError) as exc:
            self._evict(key, task)
            raise RequestError("venue request failed") from exc

    def _completed(self, key: str, task: asyncio.Task[Any]) -> None:
        _observe_task_result(task)
        cached = self._cache.get(key)
        if cached is None or cached[1] is not task:
            return
        if task.cancelled() or task.exception() is not None or self.cache_ttl_seconds == 0:
            self._evict(key, task)
            return
        # Expire even if a timestamped request key is never used again. Pending
        # requests stay shared until completion; the response TTL starts then.
        self._cache[key] = time.monotonic(), task
        self._expiry[key] = asyncio.get_running_loop().call_later(
            self.cache_ttl_seconds, self._evict, key, task
        )

    def _evict(self, key: str, task: asyncio.Task[Any]) -> None:
        cached = self._cache.get(key)
        if cached is not None and cached[1] is task:
            self._cache.pop(key, None)
            handle = self._expiry.pop(key, None)
            if handle is not None:
                handle.cancel()

    def invalidate_get(
        self, url: str, params: dict[str, Any] | None = None, *, response: Any
    ) -> None:
        """Discard a rejected response without evicting a newer request."""
        key = f"GET:{url}:{json.dumps(params or {}, sort_keys=True, separators=(',', ':'))}"
        cached = self._cache.get(key)
        if cached is None:
            return
        task = cached[1]
        if task.done() and not task.cancelled() and task.exception() is None:
            if task.result() is response:
                self._evict(key, task)



def _observe_task_result(task: asyncio.Task[Any]) -> None:
    """Retrieve background completion after a shielded waiter is cancelled."""

    if not task.cancelled():
        task.exception()
