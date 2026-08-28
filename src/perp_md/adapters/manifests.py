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
    REST_PRODUCT_FAMILY,
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


def _scenario(
    direction: str,
    instrument_kind: InstrumentKind = InstrumentKind.PERPETUAL_SWAP,
):
    unit = ContractValueUnit.BASE if direction == "linear" else ContractValueUnit.QUOTE
    settlement = (
        AssetRelationship.QUOTE if direction == "linear" else AssetRelationship.BASE
    )
    return instrument_scenario(
        InstrumentDescriptorV1(
            instrument_kind=instrument_kind,
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
        DataPointKind.FUNDING_NEXT_RATE: MeasurementUnit.RATE_FRACTION,
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
    state: DeclaredState = DeclaredState.SUPPORTED,
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
        state=state,
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
    instrument_kind: InstrumentKind = InstrumentKind.PERPETUAL_SWAP,
) -> NativeProductMapping:
    kind_name = (
        "perpetual"
        if instrument_kind is InstrumentKind.PERPETUAL_SWAP
        else "future"
    )
    return NativeProductMapping(
        mapping_id=f"{provider.lower()}.{family}.{direction}.{kind_name}.v1",
        adapter_id=adapter,
        provider_id=provider,
        family_id=family,
        native_names=tuple(
            NativeName(name, native_name_context) for name in native_names
        ),
        instrument_scenario=_scenario(direction, instrument_kind),
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


def _binance_future(direction: str) -> NativeProductMapping:
    prefix = f"binance.future.{direction}"
    quantity = (
        _native_base(prefix, TemporalMode.CURRENT, CURRENT)
        if direction == "linear"
        else _native_count(prefix, TemporalMode.CURRENT, CURRENT)
    )
    history_identity = (
        _NATIVE_SYMBOL
        if direction == "linear"
        else (*_NATIVE_SYMBOL, REST_PAIR, REST_PRODUCT_FAMILY)
    )
    history = (
        _native_base(
            prefix,
            TemporalMode.HISTORICAL,
            BOUNDED_HISTORY,
            identity=history_identity,
        )
        if direction == "linear"
        else _capability(
            f"{prefix}.open-interest.contract-count.historical",
            DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
            TemporalMode.HISTORICAL,
            "sumOpenInterest",
            BOUNDED_HISTORY,
            identity=history_identity,
        ),
        _notional(
            prefix,
            TemporalMode.HISTORICAL,
            BOUNDED_HISTORY,
            source="sumOpenInterestValue" if direction == "linear" else "sumOpenInterest",
            identity=history_identity,
            instrument=_CONTRACT_VALUE if direction == "inverse" else (),
            limitations=("history is retained for a bounded provider window",),
        ),
    )
    return _mapping(
        "BINANCE",
        "native.binance",
        "usd-m" if direction == "linear" else "coin-m",
        direction,
        ("USD-M",) if direction == "linear" else ("COIN-M",),
        (
            quantity,
            _notional(
                prefix,
                TemporalMode.CURRENT,
                CURRENT,
                source="openInterest",
                converted=direction == "linear",
                instrument=_CONTRACT_VALUE if direction == "inverse" else (),
                observations=("mark_price",) if direction == "linear" else (),
            ),
            *history,
        ),
        instrument_kind=InstrumentKind.FUTURE,
    )


def _bybit(
    direction: str,
    instrument_kind: InstrumentKind = InstrumentKind.PERPETUAL_SWAP,
) -> NativeProductMapping:
    family = direction if instrument_kind is InstrumentKind.PERPETUAL_SWAP else f"{direction}-future"
    prefix = f"bybit.{family}"
    quantity = (
        _native_base(prefix, TemporalMode.CURRENT, CURRENT),
        _native_base(prefix, TemporalMode.HISTORICAL, BOUNDED_HISTORY),
    ) if direction == "linear" else ()
    funding = (
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
    ) if instrument_kind is InstrumentKind.PERPETUAL_SWAP else ()
    return _mapping(
        "BYBIT",
        "native.bybit",
        family,
        direction,
        (direction.upper(),),
        (
            *quantity,
            _notional(
                prefix,
                TemporalMode.CURRENT,
                CURRENT,
                source="openInterestValue" if direction == "linear" else "openInterest",
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
            *funding,
        ),
        instrument_kind=instrument_kind,
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


def _gate_future() -> NativeProductMapping:
    prefix = "gate.usdt-delivery.linear"
    identity = (*_NATIVE_SYMBOL, REST_SETTLEMENT_ASSET)
    return _mapping(
        "GATE",
        "native.gate",
        "usdt-delivery",
        "linear",
        ("USDT-DELIVERY",),
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
                f"{prefix}.open-interest.base-quantity.current",
                DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
                TemporalMode.CURRENT,
                "quanto_multiplier",
                CURRENT,
                methods=((DerivationKind.PROVIDER_FORMULA, "perp_md.open_interest.gate_contracts_to_base.v1"),),
                identity=identity,
            ),
            _notional(
                prefix,
                TemporalMode.CURRENT,
                CURRENT,
                source="position_size",
                converted=True,
                identity=identity,
                observations=("mark_price",),
            ),
        ),
        instrument_kind=InstrumentKind.FUTURE,
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
            _native_count(
                prefix,
                TemporalMode.HISTORICAL,
                BOUNDED_HISTORY,
                identity=identity,
            ),
            _notional(
                prefix,
                TemporalMode.HISTORICAL,
                BOUNDED_HISTORY,
                source="open_interest",
                converted=direction == "linear",
                identity=identity,
                instrument=terms,
                observations=("mark_price_at_bucket",) if direction == "linear" else (),
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


def _native_contract_family(
    provider: str,
    family: str,
    native_name: str,
    direction: str,
    *,
    history: RetrievalDeclaration,
    adapter_id: str,
    oi_source: str,
) -> NativeProductMapping:
    prefix = f"{provider.lower()}.{direction}"
    count_current = _capability(
        f"{prefix}.open-interest.contract-count.current",
        DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
        TemporalMode.CURRENT,
        oi_source,
        CURRENT,
    )
    count_history = _capability(
        f"{prefix}.open-interest.contract-count.historical",
        DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
        TemporalMode.HISTORICAL,
        oi_source,
        history,
    )
    base_current = (
        (
            _capability(
                f"{prefix}.open-interest.base-quantity.current",
                DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
                TemporalMode.CURRENT,
                oi_source,
                CURRENT,
                methods=((DerivationKind.CANONICAL_CONVERSION, "open_interest.contracts_to_base.linear.v1"),),
                instrument=_CONTRACT_VALUE,
            ),
        )
        if direction == "linear"
        else ()
    )
    base_history = (
        (
            _capability(
                f"{prefix}.open-interest.base-quantity.historical",
                DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
                TemporalMode.HISTORICAL,
                oi_source,
                history,
                methods=((DerivationKind.CANONICAL_CONVERSION, "open_interest.contracts_to_base.linear.v1"),),
                instrument=_CONTRACT_VALUE,
            ),
        )
        if direction == "linear"
        else ()
    )
    return _mapping(
        provider,
        adapter_id,
        family,
        direction,
        (native_name,),
        (
            count_current,
            *base_current,
            _notional(
                prefix,
                TemporalMode.CURRENT,
                CURRENT,
                source=oi_source,
                converted=direction == "linear",
                instrument=_CONTRACT_VALUE,
                observations=("mark_price",) if direction == "linear" else (),
            ),
            count_history,
            *base_history,
            _notional(
                prefix,
                TemporalMode.HISTORICAL,
                history,
                source=oi_source,
                converted=direction == "linear",
                instrument=_CONTRACT_VALUE,
                observations=("mark_price_at_bucket",)
                if direction == "linear"
                else (),
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
                BOUNDED_HISTORY,
            ),
            _capability(
                f"{prefix}.funding-interval.current",
                DataPointKind.FUNDING_INTERVAL,
                TemporalMode.CURRENT,
                "funding_interval",
                CURRENT,
            ),
        ),
    )


def _native_current_family(
    provider: str,
    family: str,
    native_name: str,
    direction: str,
    *,
    adapter_id: str,
    oi_source: str,
    native_unit: str,
    instrument_kind: InstrumentKind = InstrumentKind.PERPETUAL_SWAP,
    funding_kind: DataPointKind | None = None,
    funding_history: RetrievalDeclaration | None = None,
    ccxt_funding: bool = False,
) -> NativeProductMapping:
    prefix = f"{provider.lower()}.{family}.{direction}"
    if native_unit == "base":
        quantity = (
            _capability(
                f"{prefix}.open-interest.base-quantity.current",
                DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
                TemporalMode.CURRENT,
                oi_source,
                CURRENT,
            ),
        )
        notional_instrument: tuple[str, ...] = ()
    else:
        quantity = (
            _capability(
                f"{prefix}.open-interest.contract-count.current",
                DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
                TemporalMode.CURRENT,
                oi_source,
                CURRENT,
            ),
            *(
                (
                    _converted_base(
                        prefix,
                        TemporalMode.CURRENT,
                        CURRENT,
                    ),
                )
                if direction == "linear"
                else ()
            ),
        )
        notional_instrument = _CONTRACT_VALUE
    funding = ()
    if funding_kind is not None:
        temporal = (
            TemporalMode.NEXT
            if funding_kind is DataPointKind.FUNDING_NEXT_RATE
            else TemporalMode.CURRENT
        )
        funding = (
            _funding(prefix, funding_kind, temporal, CURRENT),
            *(
                (_funding(prefix, DataPointKind.FUNDING_SETTLED_RATE, TemporalMode.HISTORICAL, funding_history),)
                if funding_history is not None
                else ()
            ),
            _capability(
                f"{prefix}.funding-interval.current",
                DataPointKind.FUNDING_INTERVAL,
                TemporalMode.CURRENT,
                "funding_interval",
                CURRENT,
            ),
        )
    elif ccxt_funding:
        funding = (
            _funding(
                prefix,
                DataPointKind.FUNDING_INDICATIVE_RATE,
                TemporalMode.CURRENT,
                RUNTIME_CURRENT,
                runtime=_CCXT_FUNDING_CURRENT_RUNTIME,
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.HISTORICAL,
                RUNTIME_HISTORY,
                runtime=_CCXT_FUNDING_HISTORY_RUNTIME,
            ),
        )
    return _mapping(
        provider,
        adapter_id,
        family,
        direction,
        (native_name,),
        (
            *quantity,
            _notional(
                prefix,
                TemporalMode.CURRENT,
                CURRENT,
                source=oi_source,
                converted=native_unit == "base" or direction == "linear",
                instrument=notional_instrument,
                observations=("mark_price",)
                if native_unit == "base" or direction == "linear"
                else (),
            ),
            *funding,
        ),
        instrument_kind=instrument_kind,
    )


def _native_dated_contract_family(
    provider: str,
    family: str,
    native_name: str,
    direction: str,
    *,
    adapter_id: str,
    oi_source: str,
    history: RetrievalDeclaration | None,
) -> NativeProductMapping:
    prefix = f"{provider.lower()}.{family}.{direction}"
    base = (
        _converted_base(prefix, TemporalMode.CURRENT, CURRENT),
    ) if direction == "linear" else ()
    historical = ()
    if history is not None:
        historical = (
            _capability(
                f"{prefix}.open-interest.contract-count.historical",
                DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
                TemporalMode.HISTORICAL,
                oi_source,
                history,
            ),
            *(
                (_converted_base(prefix, TemporalMode.HISTORICAL, history),)
                if direction == "linear"
                else ()
            ),
            _notional(
                prefix,
                TemporalMode.HISTORICAL,
                history,
                source=oi_source,
                converted=direction == "linear",
                instrument=_CONTRACT_VALUE,
                observations=("mark_price_at_bucket",)
                if direction == "linear"
                else (),
            ),
        )
    return _mapping(
        provider,
        adapter_id,
        family,
        direction,
        (native_name,),
        (
            _capability(
                f"{prefix}.open-interest.contract-count.current",
                DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
                TemporalMode.CURRENT,
                oi_source,
                CURRENT,
            ),
            *base,
            _notional(
                prefix,
                TemporalMode.CURRENT,
                CURRENT,
                source=oi_source,
                converted=direction == "linear",
                instrument=_CONTRACT_VALUE,
                observations=("mark_price",) if direction == "linear" else (),
            ),
            *historical,
        ),
        instrument_kind=InstrumentKind.FUTURE,
    )


def _htx(direction: str) -> NativeProductMapping:
    prefix = f"htx.{direction}"
    family = "usdt-m-swap" if direction == "linear" else "coin-m-swap"
    name = "USDT-M SWAP" if direction == "linear" else "COIN-M SWAP"
    count = _capability(
        f"{prefix}.open-interest.contract-count.current",
        DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
        TemporalMode.CURRENT,
        "volume",
        CURRENT,
    )
    historical_count = _capability(
        f"{prefix}.open-interest.contract-count.historical",
        DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
        TemporalMode.HISTORICAL,
        "volume",
        SINGLE_PAGE_HISTORY,
    )
    base = (
        _capability(
            f"{prefix}.open-interest.base-quantity.current",
            DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
            TemporalMode.CURRENT,
            "amount",
            CURRENT,
        ),
    ) if direction == "linear" else ()
    historical_base = (
        _capability(
            f"{prefix}.open-interest.base-quantity.historical",
            DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
            TemporalMode.HISTORICAL,
            "amount",
            SINGLE_PAGE_HISTORY,
        ),
    ) if direction == "linear" else ()
    return _mapping(
        "HTX",
        "native.htx",
        family,
        direction,
        (name,),
        (
            count,
            *base,
            historical_count,
            *historical_base,
            _notional(
                prefix,
                TemporalMode.CURRENT,
                CURRENT,
                source="value" if direction == "linear" else "volume",
                converted=direction == "inverse",
                instrument=_CONTRACT_VALUE if direction == "inverse" else (),
            ),
            _notional(
                prefix,
                TemporalMode.HISTORICAL,
                SINGLE_PAGE_HISTORY,
                source="value" if direction == "linear" else "volume",
                converted=direction == "inverse",
                instrument=_CONTRACT_VALUE if direction == "inverse" else (),
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_NEXT_RATE,
                TemporalMode.NEXT,
                CURRENT,
            ),
            _funding(
                prefix,
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.HISTORICAL,
                FULL_HISTORY,
            ),
        ),
    )


def _htx_future(direction: str) -> NativeProductMapping:
    prefix = f"htx.future.{direction}"
    history = (
        (
            _capability(
                f"{prefix}.open-interest.contract-count.historical",
                DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
                TemporalMode.HISTORICAL,
                "volume",
                SINGLE_PAGE_HISTORY,
            ),
            _converted_base(prefix, TemporalMode.HISTORICAL, SINGLE_PAGE_HISTORY),
            _notional(
                prefix,
                TemporalMode.HISTORICAL,
                SINGLE_PAGE_HISTORY,
                source="value",
            ),
        )
        if direction == "linear"
        else (
            _capability(
                f"{prefix}.open-interest.contract-count.historical",
                DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
                TemporalMode.HISTORICAL,
                "volume",
                SINGLE_PAGE_HISTORY,
                limitations=("history is provided at an hourly cadence",),
            ),
            _notional(
                prefix,
                TemporalMode.HISTORICAL,
                SINGLE_PAGE_HISTORY,
                source="volume",
                converted=True,
                instrument=_CONTRACT_VALUE,
                limitations=("history is provided at an hourly cadence",),
            ),
        )
    )
    return _mapping(
        "HTX",
        "native.htx",
        "usdt-m-futures" if direction == "linear" else "coin-m-futures",
        direction,
        ("USDT-M FUTURES",) if direction == "linear" else ("COIN-M FUTURES",),
        (
            _capability(
                f"{prefix}.open-interest.contract-count.current",
                DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
                TemporalMode.CURRENT,
                "volume",
                CURRENT,
            ),
            *(
                (
                    _capability(
                        f"{prefix}.open-interest.base-quantity.current",
                        DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
                        TemporalMode.CURRENT,
                        "amount",
                        CURRENT,
                    ),
                )
                if direction == "linear"
                else ()
            ),
            _notional(
                prefix,
                TemporalMode.CURRENT,
                CURRENT,
                source="value" if direction == "linear" else "volume",
                converted=direction == "inverse",
                instrument=_CONTRACT_VALUE if direction == "inverse" else (),
            ),
            *history,
        ),
        instrument_kind=InstrumentKind.FUTURE,
    )


def _btse(instrument_kind: InstrumentKind) -> NativeProductMapping:
    family = "swap" if instrument_kind is InstrumentKind.PERPETUAL_SWAP else "future"
    prefix = f"btse.{family}.linear"
    return _mapping(
        "BTSE",
        "native.btse",
        family,
        "linear",
        ("SWAP",) if instrument_kind is InstrumentKind.PERPETUAL_SWAP else ("FUTURE",),
        (
            _capability(
                f"{prefix}.open-interest.contract-count.current",
                DataPointKind.OPEN_INTEREST_CONTRACT_COUNT,
                TemporalMode.CURRENT,
                "openInterest",
                CURRENT,
            ),
            _capability(
                f"{prefix}.open-interest.base-quantity.current",
                DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
                TemporalMode.CURRENT,
                "openInterest",
                CURRENT,
                methods=((DerivationKind.CANONICAL_CONVERSION, "open_interest.contracts_to_base.linear.v1"),),
                instrument=_CONTRACT_VALUE,
            ),
            _notional(
                prefix,
                TemporalMode.CURRENT,
                CURRENT,
                source="openInterest",
                converted=True,
                instrument=_CONTRACT_VALUE,
                observations=("mark_price",),
            ),
            *(
                (
                    _funding(prefix, DataPointKind.FUNDING_NEXT_RATE, TemporalMode.NEXT, CURRENT),
                    _funding(prefix, DataPointKind.FUNDING_SETTLED_RATE, TemporalMode.HISTORICAL, BOUNDED_SINGLE_PAGE),
                    _capability(
                        f"{prefix}.funding-interval.current",
                        DataPointKind.FUNDING_INTERVAL,
                        TemporalMode.CURRENT,
                        "fundingIntervalMinutes",
                        CURRENT,
                    ),
                )
                if instrument_kind is InstrumentKind.PERPETUAL_SWAP
                else ()
            ),
        ),
        instrument_kind=instrument_kind,
    )


def _okx(
    direction: str,
    instrument_kind: InstrumentKind = InstrumentKind.PERPETUAL_SWAP,
) -> NativeProductMapping:
    family = "swap" if instrument_kind is InstrumentKind.PERPETUAL_SWAP else "futures"
    prefix = f"okx.{family}.{direction}"
    funding = (
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
    ) if instrument_kind is InstrumentKind.PERPETUAL_SWAP else ()
    return _mapping(
        "OKX",
        "native.okx",
        family,
        direction,
        ("SWAP",) if instrument_kind is InstrumentKind.PERPETUAL_SWAP else ("FUTURES",),
        (
            _native_count(prefix, TemporalMode.CURRENT, CURRENT),
            _native_base(prefix, TemporalMode.CURRENT, CURRENT),
            _native_base(prefix, TemporalMode.HISTORICAL, BOUNDED_HISTORY),
            _notional(prefix, TemporalMode.CURRENT, CURRENT, source="oiUsd"),
            _notional(
                prefix,
                TemporalMode.HISTORICAL,
                BOUNDED_HISTORY,
                source="oiUsd",
            ),
            *funding,
        ),
        instrument_kind=instrument_kind,
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


def _kraken_future(direction: str) -> NativeProductMapping:
    prefix = f"kraken.future.{direction}"
    quantity = (
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
            quantity,
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
        ),
        instrument_kind=InstrumentKind.FUTURE,
    )


def _optional_mapping(
    provider: str,
    direction: str,
    family: str,
    native_name: str,
    instrument_kind: InstrumentKind = InstrumentKind.PERPETUAL_SWAP,
) -> NativeProductMapping:
    prefix = f"{provider.lower()}.{family}.{direction}.{instrument_kind.value}"
    oi_identity = (
        (REST_INSTRUMENT_CATALOG_INSTRUMENT,)
        if provider == "COINBASE"
        else _NATIVE_SYMBOL
    )
    oi_runtime = (
        (CCXT_SPECIALIZED_OPEN_INTEREST_FEATURE,)
        if provider == "COINBASE"
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
        if direction == "linear" and provider != "BINGX"
        else ()
    )
    runtime_limit = (
        "availability and history shape are determined by the installed optional provider runtime",
    )
    amount_only = provider in {"BTSE", "WEEX"}
    value_only = provider == "BINGX"
    current_count = () if value_only else (
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
    )
    current_notional = _capability(
        f"{prefix}.open-interest.notional.current",
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        TemporalMode.CURRENT,
        "openInterestAmount" if amount_only else "openInterestValue",
        RUNTIME_CURRENT,
        state=DeclaredState.CONDITIONAL,
        methods=(
            (
                DerivationKind.CANONICAL_CONVERSION,
                "open_interest.contracts_to_reporting.at_mark.v1",
            ),
        )
        if amount_only
        else (),
        identity=oi_identity,
        instrument=_CONTRACT_VALUE if amount_only else (),
        observations=("mark_price",) if amount_only else (),
        runtime=oi_runtime,
        limitations=runtime_limit,
    )
    return _mapping(
        provider,
        "optional.ccxt",
        family,
        direction,
        (native_name,),
        (
            *current_count,
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
            current_notional,
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
                if provider != "COINBASE" and instrument_kind is InstrumentKind.PERPETUAL_SWAP
                else ()
            ),
            *(
            (_capability(
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
            ),)
            if instrument_kind is InstrumentKind.PERPETUAL_SWAP
            else ()
            ),
        ),
        instrument_kind=instrument_kind,
    )


def _ccxt_funding_mapping(
    provider: str,
    direction: str,
    *,
    history: bool,
    family: str = "swap",
    native_name: str = "SWAP",
) -> NativeProductMapping:
    prefix = f"{provider.lower()}.{family}.{direction}"
    runtime_limit = (
        "availability is determined by the installed optional provider runtime",
    )
    historical = (
        _funding(
            prefix,
            DataPointKind.FUNDING_SETTLED_RATE,
            TemporalMode.HISTORICAL,
            RUNTIME_HISTORY,
            runtime=_CCXT_FUNDING_HISTORY_RUNTIME,
            limitations=runtime_limit,
            state=DeclaredState.CONDITIONAL,
        ),
    ) if history else ()
    return _mapping(
        provider,
        "optional.ccxt",
        family,
        direction,
        (native_name,),
        (
            _funding(
                prefix,
                DataPointKind.FUNDING_INDICATIVE_RATE,
                TemporalMode.CURRENT,
                RUNTIME_CURRENT,
                runtime=_CCXT_FUNDING_CURRENT_RUNTIME,
                limitations=runtime_limit,
                state=DeclaredState.CONDITIONAL,
            ),
            *historical,
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
    ("BINGX", "linear", "swap", "SWAP"),
    ("BINGX", "inverse", "swap", "SWAP"),
    ("BITGET", "linear", "usdt-m", "USDT-M"),
    ("BITGET", "linear", "usdc-m", "USDC-M"),
    ("BITGET", "inverse", "coin-m", "COIN-M"),
    ("BITMART", "linear", "usdt-m", "USDT-M"),
    ("BITMART", "linear", "usdc-m", "USDC-M"),
    ("BITMART", "inverse", "coin-m", "COIN-M"),
    ("COINBASE", "linear", "intx", "INTX"),
    ("DERIBIT", "linear", "linear", "LINEAR"),
    ("DERIBIT", "inverse", "reversed", "REVERSED"),
    ("WEEX", "linear", "swap", "SWAP"),
)

_OPTIONAL_FUTURE_FAMILIES = (
    ("BITGET", "inverse", "coin-m", "COIN-M"),
    ("DERIBIT", "linear", "linear", "LINEAR"),
    ("DERIBIT", "inverse", "reversed", "REVERSED"),
)


def _manifest(
    provider: str, mappings: tuple[NativeProductMapping, ...]
) -> AdapterManifest:
    return AdapterManifest(provider, mappings)


BUILTIN_ADAPTER_MANIFESTS = (
    _manifest(
        "BINANCE",
        (
            _binance("linear"),
            _binance("inverse"),
            _binance_future("linear"),
            _binance_future("inverse"),
        ),
    ),
    _manifest(
        "BYBIT",
        (
            _bybit("linear"),
            _bybit("inverse"),
            _bybit("linear", InstrumentKind.FUTURE),
            _bybit("inverse", InstrumentKind.FUTURE),
        ),
    ),
    _manifest("GATE", (_gate("linear"), _gate("inverse"), _gate_future())),
    _manifest("BITFINEX", (_bitfinex("linear"), _bitfinex("inverse"))),
    _manifest(
        "DEEPCOIN",
        (
            _native_contract_family(
                "DEEPCOIN", "swap", "SWAP", "linear", history=BOUNDED_HISTORY, adapter_id="native.deepcoin", oi_source="oi"
            ),
            _native_contract_family(
                "DEEPCOIN", "swap", "SWAP", "inverse", history=BOUNDED_HISTORY, adapter_id="native.deepcoin", oi_source="oi"
            ),
        ),
    ),
    _manifest(
        "KUCOIN",
        (
            _native_contract_family(
                "KUCOIN", "ffwcsx", "FFWCSX", "linear", history=BOUNDED_HISTORY, adapter_id="native.kucoin", oi_source="openInterest"
            ),
            _native_contract_family(
                "KUCOIN", "ffwcsx", "FFWCSX", "inverse", history=BOUNDED_HISTORY, adapter_id="native.kucoin", oi_source="openInterest"
            ),
            _native_dated_contract_family(
                "KUCOIN",
                "fficsx",
                "FFICSX",
                "inverse",
                adapter_id="native.kucoin",
                oi_source="openInterest",
                history=BOUNDED_HISTORY,
            ),
        ),
    ),
    _manifest(
        "HTX",
        (
            _htx("linear"),
            _htx("inverse"),
            _htx_future("linear"),
            _htx_future("inverse"),
        ),
    ),
    _manifest(
        "TOOBIT",
        (_native_current_family(
            "TOOBIT", "swap", "SWAP", "linear",
            adapter_id="native.toobit", oi_source="size", native_unit="base",
            funding_kind=DataPointKind.FUNDING_NEXT_RATE,
            funding_history=BOUNDED_HISTORY,
        ),),
    ),
    _manifest(
        "PHEMEX",
        (
            _native_current_family(
                "PHEMEX", "swap", "SWAP", "linear",
                adapter_id="native.phemex", oi_source="openInterestRv",
                native_unit="base",
                ccxt_funding=True,
            ),
            _native_current_family(
                "PHEMEX", "swap", "SWAP", "inverse",
                adapter_id="native.phemex", oi_source="openInterest",
                native_unit="contracts",
                ccxt_funding=True,
            ),
        ),
    ),
    _manifest(
        "GRVT",
        (_native_current_family(
            "GRVT", "swap", "SWAP", "linear",
            adapter_id="native.grvt", oi_source="open_interest", native_unit="base",
            funding_kind=DataPointKind.FUNDING_INDICATIVE_RATE,
            funding_history=BOUNDED_HISTORY,
        ),),
    ),
    _manifest(
        "LIGHTER",
        (_native_current_family(
            "LIGHTER", "swap", "SWAP", "linear",
            adapter_id="native.lighter", oi_source="open_interest",
            native_unit="base",
            funding_kind=DataPointKind.FUNDING_INDICATIVE_RATE,
            funding_history=BOUNDED_SINGLE_PAGE,
        ),),
    ),
    _manifest(
        "BTSE",
        (
            _btse(InstrumentKind.PERPETUAL_SWAP),
            _btse(InstrumentKind.FUTURE),
        ),
    ),
    _manifest(
        "OKX",
        (
            _okx("linear"),
            _okx("inverse"),
            _okx("linear", InstrumentKind.FUTURE),
            _okx("inverse", InstrumentKind.FUTURE),
        ),
    ),
    _manifest("HYPERLIQUID", (_hyperliquid(False), _hyperliquid(True))),
    _manifest("MEXC", (_mexc("linear"), _mexc("inverse"))),
    _manifest(
        "KRAKEN",
        (
            _kraken("linear"),
            _kraken("inverse"),
            _kraken_future("linear"),
            _kraken_future("inverse"),
        ),
    ),
    _manifest("ASTER", (_ccxt_funding_mapping("ASTER", "linear", history=True),)),
    _manifest(
        "DIGIFINEX",
        (
            _ccxt_funding_mapping("DIGIFINEX", "linear", history=True),
            _ccxt_funding_mapping("DIGIFINEX", "inverse", history=True),
        ),
    ),
    _manifest(
        "XT",
        (
            _mapping(
                "XT",
                "native.xt",
                "perpetual",
                "linear",
                ("PERPETUAL",),
                (
                    _notional(
                        "xt.perpetual.linear",
                        TemporalMode.CURRENT,
                        CURRENT,
                        source="openInterestUsd",
                    ),
                    _funding(
                        "xt.perpetual.linear",
                        DataPointKind.FUNDING_INDICATIVE_RATE,
                        TemporalMode.CURRENT,
                        RUNTIME_CURRENT,
                        runtime=_CCXT_FUNDING_CURRENT_RUNTIME,
                    ),
                    _funding(
                        "xt.perpetual.linear",
                        DataPointKind.FUNDING_SETTLED_RATE,
                        TemporalMode.HISTORICAL,
                        RUNTIME_HISTORY,
                        runtime=_CCXT_FUNDING_HISTORY_RUNTIME,
                    ),
                ),
            ),
        ),
    ),
    _manifest(
        "WHITEBIT",
        (
            _ccxt_funding_mapping(
                "WHITEBIT",
                "linear",
                history=True,
                family="futures",
                native_name="FUTURES",
            ),
            _ccxt_funding_mapping(
                "WHITEBIT",
                "linear",
                history=True,
                family="tradfi-futures",
                native_name="TRADFIFUTURES",
            ),
        ),
    ),
    _manifest("CRYPTOCOM", (_ccxt_funding_mapping("CRYPTOCOM", "linear", history=True),)),
    _manifest("BLOFIN", (_ccxt_funding_mapping("BLOFIN", "linear", history=True),)),
    *(
        _manifest(
            provider,
            tuple(
                _optional_mapping(provider, direction, family, native_name)
                for candidate, direction, family, native_name in _OPTIONAL_PRODUCT_FAMILIES
                if candidate == provider
            )
            + tuple(
                _optional_mapping(
                    provider,
                    direction,
                    family,
                    native_name,
                    InstrumentKind.FUTURE,
                )
                for candidate, direction, family, native_name in _OPTIONAL_FUTURE_FAMILIES
                if candidate == provider
            ),
        )
        for provider in dict.fromkeys(
            candidate for candidate, _, _, _ in _OPTIONAL_PRODUCT_FAMILIES
        )
    ),
)
