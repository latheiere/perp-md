from __future__ import annotations

from typing import Protocol

from perp_md.models import (
    HistoryRange,
    Instrument,
    OpenInterestCapabilities,
    OpenInterestResult,
)


class OpenInterestAdapter(Protocol):
    def supports(self, instrument: Instrument) -> bool: ...
    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities: ...
    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult: ...
    async def close(self) -> None: ...
