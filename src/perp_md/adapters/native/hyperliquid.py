from __future__ import annotations

from ._common import (
    DataUnavailable,
    HYPERLIQUID_SCOPED_PRODUCT_FAMILY,
    HistoryRange,
    Instrument,
    InvalidInstrument,
    InvalidResponse,
    NativeAdapter,
    NativeUnit,
    ObservationTimeKind,
    OpenInterestCapabilities,
    OpenInterestObservation,
    OpenInterestResult,
    RPC_INSTRUMENT,
    RPC_PRODUCT_FAMILY,
    ReferenceInstrument,
    ValuationMethod,
    adapter_identity,
    number,
    optional_adapter_identity,
    proven_base_quantity,
)


class HyperliquidAdapter(NativeAdapter):
    def supports(self, instrument: Instrument) -> bool:
        return instrument.venue == "HYPERLIQUID"

    def capabilities(self, instrument: Instrument) -> OpenInterestCapabilities:
        return OpenInterestCapabilities(True, False)

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None,
        *,
        include_history: bool,
    ) -> OpenInterestResult:
        scope, native_symbol = self._scope_and_symbol(instrument)
        request = {"type": "metaAndAssetCtxs"}
        if scope is not None:
            request["dex"] = scope
        payload = await self.transport.post("https://api.hyperliquid.xyz/info", request)
        if not isinstance(payload, list) or len(payload) != 2:
            raise InvalidResponse("venue returned invalid open interest")
        metadata, contexts = payload
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("universe"), list
        ):
            raise InvalidResponse("venue returned an invalid perpetual universe")
        universe = metadata["universe"]
        if not isinstance(contexts, list) or len(contexts) != len(universe):
            raise InvalidResponse(
                "venue returned misaligned perpetual metadata and contexts"
            )
        names: list[str] = []
        for row in universe:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("name"), str)
                or not row["name"]
            ):
                raise InvalidResponse(
                    "venue returned an invalid perpetual instrument identity"
                )
            names.append(row["name"])
        matches = [index for index, name in enumerate(names) if name == native_symbol]
        if len(matches) != 1:
            raise DataUnavailable(
                "instrument is missing or ambiguous in the venue perpetual universe"
            )
        context = contexts[matches[0]]
        if not isinstance(context, dict):
            raise InvalidResponse("venue returned an invalid perpetual asset context")
        if context.get("openInterest") is None or context.get("markPx") is None:
            raise InvalidResponse("venue omitted open interest or mark price")
        native, mark = number(context["openInterest"]), number(context["markPx"])
        return OpenInterestResult(
            OpenInterestObservation(
                int(self.clock() * 1000),
                native * mark,
                native,
                NativeUnit.BASE,
                mark,
                ValuationMethod.MARK_PRICE,
                proven_base_quantity(instrument, native, NativeUnit.BASE),
                ObservationTimeKind.RETRIEVED,
            )
        )

    @staticmethod
    def _scope_and_symbol(instrument: Instrument) -> tuple[str | None, str]:
        if isinstance(instrument, ReferenceInstrument):
            native_symbol = adapter_identity(
                instrument,
                RPC_INSTRUMENT,
                legacy_value=None,
            )
            scope = optional_adapter_identity(
                instrument,
                RPC_PRODUCT_FAMILY,
                legacy_value=None,
            )
            return scope, native_symbol
        parts = instrument.symbol.split(":")
        if len(parts) > 2 or any(not part for part in parts):
            raise InvalidInstrument(
                "venue-native symbol contains an invalid perpetual namespace"
            )
        symbol_scope = (
            HyperliquidAdapter._validate_scope(parts[0]) if len(parts) == 2 else None
        )
        product_scope = HyperliquidAdapter._product_scope(instrument.product)
        if (
            symbol_scope is not None
            and product_scope is not None
            and symbol_scope != product_scope
        ):
            raise InvalidInstrument(
                "venue-native symbol namespace and product scope disagree"
            )
        scope = symbol_scope or product_scope
        native_symbol = (
            instrument.symbol
            if symbol_scope is not None or scope is None
            else f"{scope}:{instrument.symbol}"
        )
        return scope, native_symbol

    @staticmethod
    def _product_scope(product: str | None) -> str | None:
        if product is None:
            return None
        if not isinstance(product, str) or not product or product != product.strip():
            raise InvalidInstrument("venue-native product descriptor is malformed")
        if product == HYPERLIQUID_SCOPED_PRODUCT_FAMILY:
            raise InvalidInstrument(
                "venue-native product descriptor omits its perpetual scope"
            )
        if ":" not in product:
            return None
        family, scope = product.split(":", 1)
        if family != HYPERLIQUID_SCOPED_PRODUCT_FAMILY:
            raise InvalidInstrument(
                "venue-native product descriptor uses an unsupported family"
            )
        return HyperliquidAdapter._validate_scope(scope)

    @staticmethod
    def _validate_scope(scope: str) -> str:
        if not scope or ":" in scope or any(character.isspace() for character in scope):
            raise InvalidInstrument("venue-native perpetual scope is malformed")
        return scope


