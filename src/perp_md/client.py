from __future__ import annotations

from collections.abc import Mapping

from perp_md.adapters.base import OpenInterestAdapter
from perp_md.adapters.ccxt import CcxtAdapter
from perp_md.adapters.native import native_adapters
from perp_md.errors import AdapterUnavailable
from perp_md.models import HistoryRange, Instrument, OpenInterestCapabilities, OpenInterestResult
from perp_md.transport import HttpxTransport, JsonTransport


class OpenInterestClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10,
        request_concurrency: int = 16,
        per_host_concurrency: int = 4,
        transport: JsonTransport | None = None,
        adapters: Mapping[str, OpenInterestAdapter] | None = None,
        enable_ccxt_fallback: bool = False,
        fallback: OpenInterestAdapter | None = None,
    ) -> None:
        self._owns_transport = transport is None
        self._transport = transport or HttpxTransport(
            timeout_seconds,
            request_concurrency,
            per_host_concurrency,
        )
        self._adapters = {
            key.upper(): value
            for key, value in (adapters or native_adapters(self._transport)).items()
        }
        self._fallback = fallback or (CcxtAdapter(timeout_seconds) if enable_ccxt_fallback else None)
        self._closed = False

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        adapter = self._select(instrument)
        return adapter.capabilities(instrument)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None = None,
        *,
        include_history: bool = True,
    ) -> OpenInterestResult:
        if self._closed:
            raise RuntimeError("client is closed")
        return await self._select(instrument).fetch(
            instrument, history, include_history=include_history
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        seen: set[int] = set()
        for adapter in [*self._adapters.values(), self._fallback]:
            if adapter is not None and id(adapter) not in seen:
                seen.add(id(adapter))
                await adapter.close()
        if self._owns_transport:
            await self._transport.close()

    async def __aenter__(self) -> "OpenInterestClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _select(self, instrument: Instrument) -> OpenInterestAdapter:
        adapter = self._adapters.get(instrument.venue)
        if adapter is not None and adapter.supports(instrument):
            return adapter
        if self._fallback is not None and self._fallback.supports(instrument):
            return self._fallback
        raise AdapterUnavailable("no open-interest adapter is configured for this venue")
