from __future__ import annotations

from cdm import (
    AssetRelationship,
    ContractValueDescriptorV1,
    ContractValueUnit,
    DataPointDefinitionV1,
    DataPointKind,
    DerivationKind,
    DerivationStepV1,
    InstrumentDescriptorV1,
    InstrumentKind,
    MeasurementLineageV1,
    MeasurementUnit,
    NativeIdentitySelectorV1,
    NotionalDenomination,
    SettlementDescriptorV1,
    TemporalMode,
    instrument_scenario,
)

from perp_md.capabilities import (
    CCXT_FUNDING_FEATURE,
    CCXT_FUNDING_HISTORY_FEATURE,
    CCXT_OPEN_INTEREST_FEATURE,
    CCXT_OPEN_INTEREST_HISTORY_FEATURE,
    CCXT_SPECIALIZED_OPEN_INTEREST_FEATURE,
    AdapterManifest,
    CapabilityDeclaration,
    CapabilityRequirements,
    DeclaredState,
    HistoryScope,
    NativeName,
    NativeProductMapping,
    PaginationMode,
    RequestScope,
    RetrievalDeclaration,
)
from perp_md.identity import (
    REST_DERIVATIVE_STATUS_INSTRUMENT,
    REST_INSTRUMENT,
    REST_INSTRUMENT_CATALOG_INSTRUMENT,
    REST_PAIR,
    REST_SETTLEMENT_ASSET,
    RPC_INSTRUMENT,
    RPC_PRODUCT_FAMILY,
)

_NATIVE_SYMBOL = (REST_INSTRUMENT,)
_CONTRACT_VALUE = ("$.contract_value.amount",)
_CCXT_RUNTIME = (CCXT_OPEN_INTEREST_FEATURE,)
_CCXT_FUNDING_CURRENT_RUNTIME = (CCXT_FUNDING_FEATURE,)
_CCXT_FUNDING_HISTORY_RUNTIME = (CCXT_FUNDING_HISTORY_FEATURE,)

CURRENT = RetrievalDeclaration(
    RequestScope.INSTRUMENT,
    HistoryScope.NONE,
    PaginationMode.NONE,
)
AGGREGATE_CURRENT = RetrievalDeclaration(
    RequestScope.PROVIDER_AGGREGATE,
    HistoryScope.NONE,
    PaginationMode.NONE,
)
BOUNDED_HISTORY = RetrievalDeclaration(
    RequestScope.INSTRUMENT,
    HistoryScope.BOUNDED,
    PaginationMode.TIME_CURSOR,
)
BOUNDED_SINGLE_PAGE = RetrievalDeclaration(
    RequestScope.INSTRUMENT,
    HistoryScope.BOUNDED,
    PaginationMode.SINGLE_PAGE,
)
SINGLE_PAGE_HISTORY = RetrievalDeclaration(
    RequestScope.INSTRUMENT,
    HistoryScope.LATEST_WINDOW,
    PaginationMode.SINGLE_PAGE,
)
FULL_HISTORY = RetrievalDeclaration(
    RequestScope.INSTRUMENT,
    HistoryScope.FULL_RETAINED,
    PaginationMode.FULL_DOWNLOAD,
)
RUNTIME_CURRENT = RetrievalDeclaration(
    RequestScope.INSTRUMENT,
    HistoryScope.NONE,
    PaginationMode.RUNTIME_DEFINED,
)
RUNTIME_HISTORY = RetrievalDeclaration(
    RequestScope.INSTRUMENT,
    HistoryScope.BOUNDED,
    PaginationMode.RUNTIME_DEFINED,
)


def _scenario(direction: str):
    unit = ContractValueUnit.BASE if direction == "linear" else ContractValueUnit.QUOTE
    settlement = (
        AssetRelationship.QUOTE if direction == "linear" else AssetRelationship.BASE
    )
    return instrument_scenario(
        InstrumentDescriptorV1(
            instrument_kind=InstrumentKind.PERPETUAL_SWAP,
            contract_value=ContractValueDescriptorV1(unit=unit),
            settlement=SettlementDescriptorV1(asset_relationship=settlement),
        )
    )


def _definition(kind: DataPointKind, temporal: TemporalMode) -> DataPointDefinitionV1:
    units = {
        DataPointKind.OPEN_INTEREST_CONTRACT_COUNT: MeasurementUnit.CONTRACT_COUNT,
        DataPointKind.OPEN_INTEREST_BASE_QUANTITY: MeasurementUnit.BASE_QUANTITY,
        DataPointKind.OPEN_INTEREST_NOTIONAL: MeasurementUnit.NOTIONAL,
        DataPointKind.FUNDING_INDICATIVE_RATE: MeasurementUnit.RATE_FRACTION,
        DataPointKind.FUNDING_SETTLED_RATE: MeasurementUnit.RATE_FRACTION,
        DataPointKind.FUNDING_INTERVAL: MeasurementUnit.DURATION_SECONDS,
    }
    return DataPointDefinitionV1(
        kind=kind,
        temporal_mode=temporal,
        unit=units[kind],
        denomination=(
            NotionalDenomination.REPORTING
            if kind is DataPointKind.OPEN_INTEREST_NOTIONAL
            else None
        ),
    )


def _lineage(
    output: DataPointDefinitionV1,
    *methods: tuple[DerivationKind, str],
) -> MeasurementLineageV1:
    return MeasurementLineageV1(
        output,
        (
            DerivationStepV1(DerivationKind.NATIVE_REPORTED),
            *(DerivationStepV1(kind, method) for kind, method in methods),
        ),
    )


def _capability(
    capability_id: str,
    kind: DataPointKind,
    temporal: TemporalMode,
    source_name: str | tuple[str, ...],
    retrieval: RetrievalDeclaration,
    *,
    state: DeclaredState = DeclaredState.SUPPORTED,
    methods: tuple[tuple[DerivationKind, str], ...] = (),
    identity: tuple[NativeIdentitySelectorV1, ...] = _NATIVE_SYMBOL,
    instrument: tuple[str, ...] = (),
    observations: tuple[str, ...] = (),
    runtime: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
) -> CapabilityDeclaration:
    output = _definition(kind, temporal)
    return CapabilityDeclaration(
        capability_id=capability_id,
        datapoint=output,
        declared_state=state,
        lineage=None
        if state is DeclaredState.UNAVAILABLE
        else _lineage(output, *methods),
        source_observations=tuple(
            NativeName(name, "provider_response_field")
            for name in ((source_name,) if isinstance(source_name, str) else source_name)
        ),
        requirements=CapabilityRequirements(
            identity_selectors=identity,
            instrument_metadata=instrument,
            market_observations=observations,
            runtime_features=runtime,
        ),
        retrieval=retrieval,
        limitations=limitations,
    )


def _native_count(
    prefix: str,
    temporal: TemporalMode,
    retrieval: RetrievalDeclaration,
    *,
    identity: tuple[NativeIdentitySelectorV1, ...] = _NATIVE_SYMBOL,
):
    return _capability(
        f"{prefix}.open-interest.contract-count.{temporal.value}",
        DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
        temporal,
        "open_interest",
        retrieval,
        identity=identity,
    )


def _native_base(
    prefix: str,
    temporal: TemporalMode,
    retrieval: RetrievalDeclaration,
    *,
    identity: tuple[NativeIdentitySelectorV1, ...] = _NATIVE_SYMBOL,
):
    return _capability(
        f"{prefix}.open-interest.base-quantity.{temporal.value}",
        DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
        temporal,
        "open_interest_base_quantity",
        retrieval,
        identity=identity,
    )


def _converted_base(
    prefix: str,
    temporal: TemporalMode,
    retrieval: RetrievalDeclaration,
    *,
    identity: tuple[NativeIdentitySelectorV1, ...] = _NATIVE_SYMBOL,
):
    return _capability(
        f"{prefix}.open-interest.base-quantity.{temporal.value}",
        DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
        temporal,
        "open_interest",
        retrieval,
        methods=(
            (
                DerivationKind.CANONICAL_CONVERSION,
                "open_interest.contracts_to_base.linear.v1",
            ),
        ),
        identity=identity,
        instrument=_CONTRACT_VALUE,
    )


def _notional(
    prefix: str,
    temporal: TemporalMode,
    retrieval: RetrievalDeclaration,
    *,
    source: str = "open_interest_notional",
    converted: bool = False,
    identity: tuple[NativeIdentitySelectorV1, ...] = _NATIVE_SYMBOL,
    instrument: tuple[str, ...] = (),
    observations: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
):
    methods = (
        (
            (
                DerivationKind.CANONICAL_CONVERSION,
                "open_interest.base_to_quote.at_mark.v1",
            ),
        )
        if converted
        else ()
    )
    return _capability(
        f"{prefix}.open-interest.notional.{temporal.value}",
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        temporal,
        source,
        retrieval,
        methods=methods,
        identity=identity,
        instrument=instrument,
        observations=observations,
        limitations=limitations,
    )


def _funding(
    prefix: str,
    kind: DataPointKind,
    temporal: TemporalMode,
    retrieval: RetrievalDeclaration,
    *,
    converted: bool = False,
    identity: tuple[NativeIdentitySelectorV1, ...] = _NATIVE_SYMBOL,
    runtime: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
):
    methods = (
        (
            (
                DerivationKind.PROVIDER_FORMULA,
                f"perp_md.funding.absolute_to_relative.{prefix.rsplit('.', 1)[-1]}.v1",
            ),
        )
        if converted
        else ()
    )
    source = "funding_absolute_amount" if converted else "funding_rate"
    return _capability(
        f"{prefix}.{kind.value.replace('.', '-')}.{temporal.value}",
        kind,
        temporal,
        source,
        retrieval,
        methods=methods,
        identity=identity,
        observations=("mark_price",) if converted else (),
        runtime=runtime,
        limitations=limitations,
    )


def _mapping(
    provider: str,
    adapter: str,
    family: str,
    direction: str,
    native_names: tuple[str, ...],
    capabilities: tuple[CapabilityDeclaration, ...],
    *,
    native_name_context: str = "provider product family",
) -> NativeProductMapping:
    return NativeProductMapping(
        mapping_id=f"{provider.lower()}.{family}.{direction}.perpetual.v1",
        adapter_id=adapter,
        provider_id=provider,
        family_id=family,
        native_names=tuple(
            NativeName(name, native_name_context) for name in native_names
        ),
        instrument_scenario=_scenario(direction),
        capabilities=capabilities,
    )


def _binance(direction: str) -> NativeProductMapping:
    prefix = f"binance.{direction}"
    identity_history = (
        _NATIVE_SYMBOL if direction == "linear" else (*_NATIVE_SYMBOL, REST_PAIR)
    )
    quantity_current = (
        _native_base(prefix, TemporalMode.CURRENT, CURRENT)
        if direction == "linear"
        else _native_count(prefix, TemporalMode.CURRENT, CURRENT)
    )
    quantity_history = (
        _native_base(prefix, TemporalMode.HISTORICAL, BOUNDED_HISTORY)
        if direction == "linear"
        else _capability(
            f"{prefix}.open-interest.contract-count.historical",
            DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
            TemporalMode.HISTORICAL,
            "sumOpenInterest",
            BOUNDED_HISTORY,
            identity=identity_history,
        )
    )
    inverse_terms = _CONTRACT_VALUE if direction == "inverse" else ()
    return _mapping(
        "BINANCE",
        "native.binance",
        "usd-m" if direction == "linear" else "coin-m",
        direction,
        ("USD-M",) if direction == "linear" else ("COIN-M",),
        (
            quantity_current,
            quantity_history,
            _notional(
                prefix,
                TemporalMode.CURRENT,
                CURRENT,
                converted=direction == "linear",
                source="openInterest",
                instrument=inverse_terms,
                observations=("mark_price",) if direction == "linear" else (),
            ),
            _notional(
                prefix,
                TemporalMode.HISTORICAL,
                BOUNDED_HISTORY,
                source="sumOpenInterestValue"
                if direction == "linear"
                else "sumOpenInterest",
                identity=identity_history,
                instrument=inverse_terms,
                limitations=("history is retained for a bounded provider window",),
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_INDICATIVE_RATE,
                TemporalMode.CURRENT,
                CURRENT,
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.HISTORICAL,
                BOUNDED_HISTORY,
            ),
            *(
                (
                    _capability(
                        f"{prefix}.funding-interval.current",
                        DataPointKind.FUNDING_INTERVAL,
                        TemporalMode.CURRENT,
                        "fundingIntervalHours",
                        CURRENT,
                    ),
                )
                if direction == "linear"
                else ()
            ),
        ),
    )


def _bybit(direction: str) -> NativeProductMapping:
    prefix = f"bybit.{direction}"
    return _mapping(
        "BYBIT",
        "native.bybit",
        direction,
        direction,
        (direction.upper(),),
        (
            _native_count(prefix, TemporalMode.CURRENT, CURRENT),
            _native_count(prefix, TemporalMode.HISTORICAL, BOUNDED_HISTORY),
            *(
                (
                    _converted_base(prefix, TemporalMode.CURRENT, CURRENT),
                    _converted_base(prefix, TemporalMode.HISTORICAL, BOUNDED_HISTORY),
                )
                if direction == "linear"
                else ()
            ),
            _notional(
                prefix, TemporalMode.CURRENT, CURRENT, source="openInterestValue"
            ),
            _notional(
                prefix,
                TemporalMode.HISTORICAL,
                BOUNDED_HISTORY,
                source="openInterest",
                converted=direction == "linear",
                observations=("mark_price_at_bucket",) if direction == "linear" else (),
                limitations=(
                    "history can be partial when supporting price buckets are absent",
                ),
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_INDICATIVE_RATE,
                TemporalMode.CURRENT,
                CURRENT,
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.HISTORICAL,
                BOUNDED_SINGLE_PAGE,
            ),
            _capability(
                f"{prefix}.funding-interval.current",
                DataPointKind.FUNDING_INTERVAL,
                TemporalMode.CURRENT,
                "fundingIntervalHour",
                CURRENT,
            ),
        ),
    )


def _gate(direction: str) -> NativeProductMapping:
    prefix = f"gate.{direction}"
    identity = (*_NATIVE_SYMBOL, REST_SETTLEMENT_ASSET)
    current_base = (
        (
            _capability(
                f"{prefix}.open-interest.base-quantity.current",
                DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
                TemporalMode.CURRENT,
                "quanto_multiplier",
                CURRENT,
                methods=(
                    (
                        DerivationKind.PROVIDER_FORMULA,
                        "perp_md.open_interest.gate_contracts_to_base.v1",
                    ),
                ),
                identity=identity,
            ),
        )
        if direction == "linear"
        else ()
    )
    history_base = (
        (
            _capability(
                f"{prefix}.open-interest.base-quantity.historical",
                DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
                TemporalMode.HISTORICAL,
                "open_interest_usd",
                BOUNDED_HISTORY,
                methods=(
                    (
                        DerivationKind.PROVIDER_FORMULA,
                        "perp_md.open_interest.reporting_notional_to_base_at_mark.v1",
                    ),
                ),
                identity=identity,
                observations=("mark_price_at_bucket",),
            ),
        )
        if direction == "linear"
        else ()
    )
    return _mapping(
        "GATE",
        "native.gate",
        "usdt-perp" if direction == "linear" else "btc-perp",
        direction,
        ("USDT-PERP",) if direction == "linear" else ("BTC-PERP",),
        (
            _capability(
                f"{prefix}.open-interest.contract-count.current",
                DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
                TemporalMode.CURRENT,
                "position_size",
                CURRENT,
                identity=identity,
            ),
            _capability(
                f"{prefix}.open-interest.contract-count.historical",
                DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
                TemporalMode.HISTORICAL,
                "open_interest",
                BOUNDED_HISTORY,
                identity=identity,
            ),
            *current_base,
            *history_base,
            _notional(
                prefix,
                TemporalMode.CURRENT,
                CURRENT,
                source="position_size",
                converted=direction == "linear",
                identity=identity,
                instrument=_CONTRACT_VALUE if direction == "inverse" else (),
                observations=("mark_price",) if direction == "linear" else (),
            ),
            _notional(
                prefix,
                TemporalMode.HISTORICAL,
                BOUNDED_HISTORY,
                source="open_interest_usd",
                identity=identity,
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_INDICATIVE_RATE,
                TemporalMode.CURRENT,
                CURRENT,
                identity=identity,
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.HISTORICAL,
                BOUNDED_SINGLE_PAGE,
                identity=identity,
            ),
            _capability(
                f"{prefix}.funding-interval.current",
                DataPointKind.FUNDING_INTERVAL,
                TemporalMode.CURRENT,
                "funding_interval",
                CURRENT,
                identity=identity,
            ),
        ),
    )


def _bitfinex(direction: str) -> NativeProductMapping:
    prefix = f"bitfinex.{direction}"
    terms = _CONTRACT_VALUE
    identity = (REST_DERIVATIVE_STATUS_INSTRUMENT,)
    base = (
        (
            _converted_base(
                prefix,
                TemporalMode.CURRENT,
                AGGREGATE_CURRENT,
                identity=identity,
            ),
        )
        if direction == "linear"
        else ()
    )
    return _mapping(
        "BITFINEX",
        "hybrid.bitfinex",
        "f0",
        direction,
        ("F0",),
        (
            _native_count(
                prefix,
                TemporalMode.CURRENT,
                AGGREGATE_CURRENT,
                identity=identity,
            ),
            *base,
            _notional(
                prefix,
                TemporalMode.CURRENT,
                AGGREGATE_CURRENT,
                source="open_interest",
                converted=direction == "linear",
                identity=identity,
                instrument=terms,
                observations=("mark_price",) if direction == "linear" else (),
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_INDICATIVE_RATE,
                TemporalMode.CURRENT,
                RUNTIME_CURRENT,
                runtime=_CCXT_FUNDING_CURRENT_RUNTIME,
                limitations=(
                    "availability is determined by the installed optional provider runtime",
                ),
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.HISTORICAL,
                RUNTIME_HISTORY,
                runtime=_CCXT_FUNDING_HISTORY_RUNTIME,
                limitations=(
                    "availability is determined by the installed optional provider runtime",
                ),
            ),
        ),
    )


def _okx(direction: str) -> NativeProductMapping:
    prefix = f"okx.{direction}"
    base = (
        (_converted_base(prefix, TemporalMode.CURRENT, CURRENT),)
        if direction == "linear"
        else ()
    )
    return _mapping(
        "OKX",
        "native.okx",
        "swap",
        direction,
        ("SWAP",),
        (
            _native_count(prefix, TemporalMode.CURRENT, CURRENT),
            *base,
            _notional(prefix, TemporalMode.CURRENT, CURRENT, source="oiUsd"),
            _funding(
                prefix,
                DataPointKind.FUNDING_INDICATIVE_RATE,
                TemporalMode.CURRENT,
                CURRENT,
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.HISTORICAL,
                SINGLE_PAGE_HISTORY,
            ),
            _capability(
                f"{prefix}.funding-interval.current",
                DataPointKind.FUNDING_INTERVAL,
                TemporalMode.CURRENT,
                ("fundingTime", "nextFundingTime"),
                CURRENT,
            ),
        ),
    )


def _hyperliquid(scoped: bool) -> NativeProductMapping:
    prefix = "hyperliquid.linear"
    oi_identity = (RPC_INSTRUMENT, RPC_PRODUCT_FAMILY) if scoped else (RPC_INSTRUMENT,)
    funding_identity = (RPC_INSTRUMENT,)
    return _mapping(
        "HYPERLIQUID",
        "native.hyperliquid",
        "hip-3-scoped" if scoped else "perp",
        "linear",
        ("HIP-3:<scope>",) if scoped else ("PERP",),
        (
            _native_base(
                prefix, TemporalMode.CURRENT, AGGREGATE_CURRENT, identity=oi_identity
            ),
            _notional(
                prefix,
                TemporalMode.CURRENT,
                AGGREGATE_CURRENT,
                source="openInterest",
                converted=True,
                identity=oi_identity,
                observations=("mark_price",),
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.SETTLED,
                BOUNDED_SINGLE_PAGE,
                identity=funding_identity,
                limitations=(
                    "current acquisition returns the latest settled rate",
                    "the funding rate settles on a documented hourly frequency",
                ),
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.HISTORICAL,
                BOUNDED_SINGLE_PAGE,
                identity=funding_identity,
                limitations=(
                    "the funding rate settles on a documented hourly frequency",
                ),
            ),
        ),
        native_name_context=(
            "provider product family template" if scoped else "provider product family"
        ),
    )


def _mexc(direction: str) -> NativeProductMapping:
    prefix = f"mexc.{direction}"
    base = (
        (_converted_base(prefix, TemporalMode.CURRENT, AGGREGATE_CURRENT),)
        if direction == "linear"
        else ()
    )
    return _mapping(
        "MEXC",
        "hybrid.mexc",
        "perp",
        direction,
        ("PERP",),
        (
            _native_count(prefix, TemporalMode.CURRENT, AGGREGATE_CURRENT),
            *base,
            _notional(
                prefix,
                TemporalMode.CURRENT,
                AGGREGATE_CURRENT,
                source="holdVol",
                converted=direction == "linear",
                instrument=_CONTRACT_VALUE,
                observations=("fair_price",) if direction == "linear" else (),
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_INDICATIVE_RATE,
                TemporalMode.CURRENT,
                RUNTIME_CURRENT,
                runtime=_CCXT_FUNDING_CURRENT_RUNTIME,
                limitations=(
                    "availability is determined by the installed optional provider runtime",
                ),
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.HISTORICAL,
                RUNTIME_HISTORY,
                runtime=_CCXT_FUNDING_HISTORY_RUNTIME,
                limitations=(
                    "availability is determined by the installed optional provider runtime",
                ),
            ),
        ),
    )


def _kraken(direction: str) -> NativeProductMapping:
    prefix = f"kraken.{direction}"
    current_quantity = (
        _native_base(prefix, TemporalMode.CURRENT, AGGREGATE_CURRENT)
        if direction == "linear"
        else _native_count(prefix, TemporalMode.CURRENT, AGGREGATE_CURRENT)
    )
    historical_quantity = (
        _native_base(prefix, TemporalMode.HISTORICAL, BOUNDED_HISTORY)
        if direction == "linear"
        else _native_count(prefix, TemporalMode.HISTORICAL, BOUNDED_HISTORY)
    )
    return _mapping(
        "KRAKEN",
        "native.kraken",
        "flexible-futures" if direction == "linear" else "futures-inverse",
        direction,
        ("FLEXIBLE_FUTURES",) if direction == "linear" else ("FUTURES_INVERSE",),
        (
            current_quantity,
            historical_quantity,
            _notional(
                prefix,
                TemporalMode.CURRENT,
                AGGREGATE_CURRENT,
                source="openInterest",
                converted=direction == "linear",
                instrument=_CONTRACT_VALUE if direction == "inverse" else (),
                observations=("mark_price",) if direction == "linear" else (),
            ),
            _notional(
                prefix,
                TemporalMode.HISTORICAL,
                BOUNDED_HISTORY,
                source="open_interest",
                converted=direction == "linear",
                instrument=_CONTRACT_VALUE if direction == "inverse" else (),
                observations=("mark_price_at_bucket",) if direction == "linear" else (),
                limitations=(
                    "history is retained for a bounded provider window",
                    "history can be partial when supporting price buckets are absent",
                )
                if direction == "linear"
                else ("history is retained for a bounded provider window",),
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_INDICATIVE_RATE,
                TemporalMode.CURRENT,
                AGGREGATE_CURRENT,
                converted=True,
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.HISTORICAL,
                FULL_HISTORY,
                limitations=(
                    "an explicit history start is required",
                    "the funding rate is standardized to a documented hourly frequency",
                ),
            ),
        ),
    )


def _optional_mapping(
    provider: str,
    direction: str,
    family: str,
    native_name: str,
) -> NativeProductMapping:
    prefix = f"{provider.lower()}.{direction}"
    oi_identity = (
        (REST_INSTRUMENT_CATALOG_INSTRUMENT,)
        if provider == "COINBASE"
        else _NATIVE_SYMBOL
    )
    oi_runtime = (
        (CCXT_SPECIALIZED_OPEN_INTEREST_FEATURE,)
        if provider in {"COINBASE", "WHITEBIT"}
        else _CCXT_RUNTIME
    )
    base = (
        (
            _converted_base(
                prefix,
                TemporalMode.CURRENT,
                RUNTIME_CURRENT,
                identity=oi_identity,
            ),
        )
        if direction == "linear"
        else ()
    )
    runtime_limit = (
        "availability and history shape are determined by the installed optional provider runtime",
    )
    return _mapping(
        provider,
        "optional.ccxt",
        family,
        direction,
        (native_name,),
        (
            _capability(
                f"{prefix}.open-interest.contract-count.current",
                DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
                TemporalMode.CURRENT,
                "openInterestAmount",
                RUNTIME_CURRENT,
                state=DeclaredState.CONDITIONAL,
                identity=oi_identity,
                runtime=oi_runtime,
                limitations=runtime_limit,
            ),
            *tuple(
                CapabilityDeclaration(
                    capability_id=item.capability_id,
                    datapoint=item.datapoint,
                    declared_state=DeclaredState.CONDITIONAL,
                    lineage=item.lineage,
                    source_observations=item.source_observations,
                    requirements=CapabilityRequirements(
                        identity_selectors=item.requirements.identity_selectors,
                        instrument_metadata=item.requirements.instrument_metadata,
                        market_observations=item.requirements.market_observations,
                        runtime_features=oi_runtime,
                    ),
                    retrieval=item.retrieval,
                    limitations=runtime_limit,
                )
                for item in base
            ),
            _capability(
                f"{prefix}.open-interest.notional.current",
                DataPointKind.OPEN_INTEREST_NOTIONAL,
                TemporalMode.CURRENT,
                "openInterestValue",
                RUNTIME_CURRENT,
                state=DeclaredState.CONDITIONAL,
                identity=oi_identity,
                runtime=oi_runtime,
                limitations=runtime_limit,
            ),
            *(
                (
                    _capability(
                        f"{prefix}.open-interest.notional.historical",
                        DataPointKind.OPEN_INTEREST_NOTIONAL,
                        TemporalMode.HISTORICAL,
                        "openInterestValue",
                        RUNTIME_HISTORY,
                        state=DeclaredState.CONDITIONAL,
                        identity=_NATIVE_SYMBOL,
                        runtime=(CCXT_OPEN_INTEREST_HISTORY_FEATURE,),
                        limitations=runtime_limit,
                    ),
                )
                if provider not in {"COINBASE", "WHITEBIT"}
                else ()
            ),
            _capability(
                f"{prefix}.funding-indicative.current",
                DataPointKind.FUNDING_INDICATIVE_RATE,
                TemporalMode.CURRENT,
                "fundingRate",
                RUNTIME_CURRENT,
                state=DeclaredState.CONDITIONAL,
                runtime=_CCXT_FUNDING_CURRENT_RUNTIME,
                limitations=runtime_limit,
            ),
            _capability(
                f"{prefix}.funding-settled.historical",
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.HISTORICAL,
                "fundingRate",
                RUNTIME_HISTORY,
                state=DeclaredState.CONDITIONAL,
                runtime=_CCXT_FUNDING_HISTORY_RUNTIME,
                limitations=runtime_limit,
            ),
            _capability(
                f"{prefix}.funding-interval.current",
                DataPointKind.FUNDING_INTERVAL,
                TemporalMode.CURRENT,
                ("interval", "fundingInterval", "funding_interval_minutes"),
                RUNTIME_CURRENT,
                state=DeclaredState.CONDITIONAL,
                runtime=_CCXT_FUNDING_CURRENT_RUNTIME,
                limitations=runtime_limit,
            ),
        ),
    )


_OPTIONAL_PRODUCT_FAMILIES = (
    ("BITGET", "linear", "usdt-m", "USDT-M"),
    ("BITGET", "linear", "usdc-m", "USDC-M"),
    ("BITGET", "inverse", "coin-m", "COIN-M"),
    ("BITMART", "linear", "usdt-m", "USDT-M"),
    ("BITMART", "linear", "usdc-m", "USDC-M"),
    ("BITMART", "inverse", "coin-m", "COIN-M"),
    ("COINBASE", "linear", "intx", "INTX"),
    ("DERIBIT", "linear", "linear", "LINEAR"),
    ("DERIBIT", "inverse", "reversed", "REVERSED"),
    ("HTX", "linear", "usdt-m-swap", "USDT-M SWAP"),
    ("HTX", "inverse", "coin-m-swap", "COIN-M SWAP"),
    ("KUCOIN", "linear", "ffwcsx", "FFWCSX"),
    ("KUCOIN", "inverse", "ffwcsx", "FFWCSX"),
    ("WHITEBIT", "linear", "futures", "FUTURES"),
    ("WHITEBIT", "linear", "tradfi-futures", "TRADFIFUTURES"),
    ("XT", "linear", "perpetual", "PERPETUAL"),
)


def _manifest(
    provider: str, mappings: tuple[NativeProductMapping, ...]
) -> AdapterManifest:
    return AdapterManifest(provider, mappings)


BUILTIN_ADAPTER_MANIFESTS = (
    _manifest("BINANCE", (_binance("linear"), _binance("inverse"))),
    _manifest("BYBIT", (_bybit("linear"), _bybit("inverse"))),
    _manifest("GATE", (_gate("linear"), _gate("inverse"))),
    _manifest("BITFINEX", (_bitfinex("linear"), _bitfinex("inverse"))),
    _manifest("OKX", (_okx("linear"), _okx("inverse"))),
    _manifest("HYPERLIQUID", (_hyperliquid(False), _hyperliquid(True))),
    _manifest("MEXC", (_mexc("linear"), _mexc("inverse"))),
    _manifest("KRAKEN", (_kraken("linear"), _kraken("inverse"))),
    *(
        _manifest(
            provider,
            tuple(
                _optional_mapping(provider, direction, family, native_name)
                for candidate, direction, family, native_name in _OPTIONAL_PRODUCT_FAMILIES
                if candidate == provider
            ),
        )
        for provider in dict.fromkeys(
            candidate for candidate, _, _, _ in _OPTIONAL_PRODUCT_FAMILIES
        )
    ),
)
