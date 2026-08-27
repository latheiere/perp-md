from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cdm import (
    AssetRelationship,
    ContractValueUnit,
    InstrumentKind,
    InstrumentReferenceV1,
    NativeIdentityNamespace,
    NativeIdentityRole,
    NativeIdentitySelectionStatus,
    NativeIdentitySelectorV1,
    NativeIdentityV1,
    select_native_identity,
)

from perp_md.errors import NativeIdentityResolutionError
from perp_md.models import ContractDirection, Instrument

REST_INSTRUMENT = NativeIdentitySelectorV1(
    NativeIdentityRole.INSTRUMENT,
    NativeIdentityNamespace.REST,
)
REST_SETTLEMENT_ASSET = NativeIdentitySelectorV1(
    NativeIdentityRole.SETTLEMENT_ASSET,
    NativeIdentityNamespace.REST,
)
REST_PAIR = NativeIdentitySelectorV1(
    NativeIdentityRole.PAIR,
    NativeIdentityNamespace.REST,
)
REST_PRODUCT_FAMILY = NativeIdentitySelectorV1(
    NativeIdentityRole.PRODUCT_FAMILY,
    NativeIdentityNamespace.REST,
)
REST_INSTRUMENT_CATALOG_INSTRUMENT = NativeIdentitySelectorV1(
    NativeIdentityRole.INSTRUMENT,
    NativeIdentityNamespace.REST_INSTRUMENT_CATALOG,
)
REST_DERIVATIVE_STATUS_INSTRUMENT = NativeIdentitySelectorV1(
    NativeIdentityRole.INSTRUMENT,
    NativeIdentityNamespace.REST_DERIVATIVE_STATUS,
)
RPC_INSTRUMENT = NativeIdentitySelectorV1(
    NativeIdentityRole.INSTRUMENT,
    NativeIdentityNamespace.RPC,
)
RPC_PRODUCT_FAMILY = NativeIdentitySelectorV1(
    NativeIdentityRole.PRODUCT_FAMILY,
    NativeIdentityNamespace.RPC,
)


def resolve_native_identity(
    reference: InstrumentReferenceV1,
    selector: NativeIdentitySelectorV1,
) -> str:
    """Resolve one exact CDM identity or raise a structured acquisition error."""

    selection = select_native_identity(reference.native_identities, selector)
    if selection.status is not NativeIdentitySelectionStatus.UNIQUE:
        raise NativeIdentityResolutionError(selection)
    identity = selection.identity
    assert identity is not None
    return identity.value


def legacy_native_identities(instrument: Instrument) -> tuple[NativeIdentityV1, ...]:
    """Project legacy exact values into the typed identity model without mutation."""

    identities = [
        NativeIdentityV1(selector.role, selector.namespace, instrument.symbol)
        for selector in (
            REST_INSTRUMENT,
            REST_INSTRUMENT_CATALOG_INSTRUMENT,
            RPC_INSTRUMENT,
        )
    ]
    if instrument.product:
        identities.append(
            NativeIdentityV1(
                RPC_PRODUCT_FAMILY.role,
                RPC_PRODUCT_FAMILY.namespace,
                instrument.product,
            )
        )
    if instrument.pair_symbol:
        identities.extend(
            NativeIdentityV1(selector.role, selector.namespace, instrument.pair_symbol)
            for selector in (
                REST_PAIR,
                REST_INSTRUMENT_CATALOG_INSTRUMENT,
                REST_DERIVATIVE_STATUS_INSTRUMENT,
            )
        )
    if instrument.settlement_currency:
        identities.append(
            NativeIdentityV1(
                REST_SETTLEMENT_ASSET.role,
                REST_SETTLEMENT_ASSET.namespace,
                instrument.settlement_currency,
            )
        )
    return tuple(identities)


@dataclass(frozen=True, slots=True)
class ReferenceInstrument:
    """Internal adapter view over an unchanged CDM instrument reference."""

    venue: str
    reference: InstrumentReferenceV1

    def __post_init__(self) -> None:
        if not self.venue or self.venue != self.venue.strip().upper():
            raise ValueError("provider_id must be a normalized non-empty identifier")
        if not isinstance(self.reference, InstrumentReferenceV1):
            raise TypeError("reference must be a CDM InstrumentReferenceV1")

    @property
    def symbol(self) -> str:
        return resolve_native_identity(self.reference, REST_INSTRUMENT)

    @property
    def market_type(self) -> str:
        kind = self.reference.descriptor.instrument_kind
        if kind is InstrumentKind.PERPETUAL_SWAP:
            return "perpetual"
        if kind is InstrumentKind.FUTURE:
            return "future"
        return ""

    @property
    def contract_direction(self) -> ContractDirection | None:
        descriptor = self.reference.descriptor
        unit = descriptor.contract_value.unit if descriptor.contract_value else None
        settlement = (
            descriptor.settlement.asset_relationship if descriptor.settlement else None
        )
        if unit is ContractValueUnit.BASE and settlement is AssetRelationship.QUOTE:
            return ContractDirection.LINEAR
        if unit is ContractValueUnit.QUOTE and settlement is AssetRelationship.BASE:
            return ContractDirection.INVERSE
        return None

    @property
    def contract_multiplier(self) -> float | None:
        value: Decimal | None = (
            self.reference.descriptor.contract_value.amount
            if self.reference.descriptor.contract_value
            else None
        )
        return float(value) if value is not None else None

    @property
    def settlement_currency(self) -> str | None:
        return _optional_identity(self.reference, REST_SETTLEMENT_ASSET)

    @property
    def pair_symbol(self) -> str | None:
        return _optional_identity(self.reference, REST_PAIR)

    @property
    def rest_product_family(self) -> str | None:
        return _optional_identity(self.reference, REST_PRODUCT_FAMILY)

    base_currency: None = None
    quote_currency: None = None
    product: None = None


def adapter_identity(
    instrument: Instrument | ReferenceInstrument,
    selector: NativeIdentitySelectorV1,
    *,
    legacy_value: str | None,
) -> str:
    """Resolve canonical references exactly while retaining legacy call behavior."""

    if isinstance(instrument, ReferenceInstrument):
        return resolve_native_identity(instrument.reference, selector)
    if legacy_value is None or not legacy_value:
        selection = select_native_identity((), selector)
        raise NativeIdentityResolutionError(selection)
    return legacy_value


def optional_adapter_identity(
    instrument: Instrument | ReferenceInstrument,
    selector: NativeIdentitySelectorV1,
    *,
    legacy_value: str | None,
) -> str | None:
    if isinstance(instrument, ReferenceInstrument):
        return _optional_identity(instrument.reference, selector)
    return legacy_value


def _optional_identity(
    reference: InstrumentReferenceV1,
    selector: NativeIdentitySelectorV1,
) -> str | None:
    selection = select_native_identity(reference.native_identities, selector)
    if selection.status is NativeIdentitySelectionStatus.MISSING:
        return None
    if selection.status is NativeIdentitySelectionStatus.AMBIGUOUS:
        raise NativeIdentityResolutionError(selection)
    identity = selection.identity
    assert identity is not None
    return identity.value
