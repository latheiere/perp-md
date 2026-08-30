from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from importlib.resources import files
from typing import Any

from cdm import (
    AssetRelationship,
    ContractValueDescriptorV1,
    ContractValueUnit,
    DataPointDefinitionV1,
    DataPointKind,
    InstrumentDescriptorV1,
    InstrumentKind,
    InstrumentReferenceV1,
    InstrumentScenarioV1,
    MeasurementLineageV1,
    NativeIdentitySelectionStatus,
    NativeIdentitySelectorV1,
    NativeIdentityV1,
    SettlementDescriptorV1,
    TemporalMode,
    select_native_identity,
    to_data,
)

from perp_md.identity import legacy_native_identities
from perp_md.models import ContractDirection, Instrument

DECLARED_COVERAGE_SCHEMA_ID = "urn:perp-md:schema:declared-coverage:1"
COVERAGE_SCHEMA_VERSION = "acquisition.coverage/v1"
MANIFEST_ID = "perp-md/acquisition-coverage/0.4.0"
MANIFEST_DECLARED_AT = "2026-08-31T00:00:00Z"
PACKAGE_VERSION = "0.4.0"
CCXT_OPEN_INTEREST_FEATURE = "ccxt.fetch_open_interest"
CCXT_OPEN_INTEREST_HISTORY_FEATURE = "ccxt.fetch_open_interest_history"
CCXT_SPECIALIZED_OPEN_INTEREST_FEATURE = "ccxt.open_interest.specialized_catalog"
CCXT_FUNDING_FEATURE = "ccxt.fetch_funding_rate"
CCXT_FUNDING_HISTORY_FEATURE = "ccxt.fetch_funding_rate_history"


class DeclaredState(StrEnum):
    SUPPORTED = "supported"
    CONDITIONAL = "conditional"
    UNAVAILABLE = "unavailable"


class RequestScope(StrEnum):
    INSTRUMENT = "instrument"
    PROVIDER_AGGREGATE = "provider_aggregate"


class HistoryScope(StrEnum):
    NONE = "none"
    LATEST_WINDOW = "latest_window"
    BOUNDED = "bounded"
    FULL_RETAINED = "full_retained"


class PaginationMode(StrEnum):
    NONE = "none"
    SINGLE_PAGE = "single_page"
    PAGE_NUMBER = "page_number"
    TIME_CURSOR = "time_cursor"
    FULL_DOWNLOAD = "full_download"
    RUNTIME_DEFINED = "runtime_defined"


class CapabilityStatus(StrEnum):
    SUPPORTED = "supported"
    METADATA_INCOMPLETE = "metadata_incomplete"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    UNSUPPORTED = "unsupported"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"


@dataclass(frozen=True, slots=True)
class NativeName:
    name: str
    context: str


@dataclass(frozen=True, slots=True)
class CapabilityRequirements:
    identity_selectors: tuple[NativeIdentitySelectorV1, ...] = ()
    instrument_metadata: tuple[str, ...] = ()
    market_observations: tuple[str, ...] = ()
    runtime_features: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalDeclaration:
    request_scope: RequestScope
    history_scope: HistoryScope
    pagination: PaginationMode


@dataclass(frozen=True, slots=True)
class CapabilityDeclaration:
    capability_id: str
    datapoint: DataPointDefinitionV1
    declared_state: DeclaredState
    lineage: MeasurementLineageV1 | None
    source_observations: tuple[NativeName, ...]
    requirements: CapabilityRequirements
    retrieval: RetrievalDeclaration
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.declared_state is DeclaredState.UNAVAILABLE:
            if self.lineage is not None:
                raise ValueError("an unavailable capability cannot declare lineage")
        elif self.lineage is None or self.lineage.output != self.datapoint:
            raise ValueError("capability lineage must produce its declared datapoint")


@dataclass(frozen=True, slots=True)
class NativeProductMapping:
    mapping_id: str
    adapter_id: str
    provider_id: str
    family_id: str
    native_names: tuple[NativeName, ...]
    instrument_scenario: InstrumentScenarioV1
    capabilities: tuple[CapabilityDeclaration, ...]


@dataclass(frozen=True, slots=True)
class AdapterManifest:
    provider_id: str
    mappings: tuple[NativeProductMapping, ...]


@dataclass(frozen=True, slots=True)
class CapabilityIssue:
    code: str
    path: str
    message: str
    identity_selector: NativeIdentitySelectorV1 | None = None


@dataclass(frozen=True, slots=True)
class CapabilityAlternative:
    mapping_id: str
    capability_id: str
    declared_state: DeclaredState
    issues: tuple[CapabilityIssue, ...]


@dataclass(frozen=True, slots=True)
class CapabilityAssessment:
    provider_id: str
    datapoint_kind: DataPointKind
    temporal_mode: TemporalMode | None
    status: CapabilityStatus
    issues: tuple[CapabilityIssue, ...] = ()
    alternatives: tuple[CapabilityAlternative, ...] = ()

    @property
    def missing_fields(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    issue.path
                    for issue in self.issues
                    if issue.code.startswith("missing_")
                }
            )
        )


@dataclass(frozen=True, slots=True)
class PlannedRetrieval:
    """Generic scheduler inputs for one assessed acquisition operation."""

    request_scope: RequestScope
    history_scope: HistoryScope
    pagination: PaginationMode
    fixed_interval_seconds: int | None = None
    max_lookback_seconds: int | None = None
    requires_explicit_start: bool = False

    def __post_init__(self) -> None:
        if self.fixed_interval_seconds is not None and self.fixed_interval_seconds <= 0:
            raise ValueError("fixed_interval_seconds must be positive when supplied")
        if self.max_lookback_seconds is not None and self.max_lookback_seconds <= 0:
            raise ValueError("max_lookback_seconds must be positive when supplied")
        if self.history_scope is HistoryScope.NONE and (
            self.fixed_interval_seconds is not None
            or self.max_lookback_seconds is not None
            or self.requires_explicit_start
        ):
            raise ValueError("current-only retrieval cannot carry history constraints")


@dataclass(frozen=True, slots=True)
class AcquisitionPlan:
    """Runtime assessment plus scheduler-safe generic retrieval properties."""

    assessment: CapabilityAssessment
    retrieval: PlannedRetrieval | None = None

    @property
    def status(self) -> CapabilityStatus:
        return self.assessment.status

    @property
    def issues(self) -> tuple[CapabilityIssue, ...]:
        return self.assessment.issues


def instrument_descriptor_from_instrument(
    instrument: Instrument,
) -> InstrumentDescriptorV1:
    """Project the legacy acquisition input into an incomplete CDM descriptor."""

    market_type = instrument.market_type.strip().lower()
    instrument_kind = (
        InstrumentKind.PERPETUAL_SWAP
        if market_type in {"perpetual", "swap"}
        else InstrumentKind.FUTURE
        if market_type == "future"
        else None
    )
    unit: ContractValueUnit | None = None
    settlement: AssetRelationship | None = None
    if instrument.contract_direction is ContractDirection.LINEAR:
        unit = ContractValueUnit.BASE
        settlement = AssetRelationship.QUOTE
    elif instrument.contract_direction is ContractDirection.INVERSE:
        unit = ContractValueUnit.QUOTE
        settlement = AssetRelationship.BASE
    amount = (
        Decimal(str(instrument.contract_multiplier))
        if instrument.contract_multiplier is not None
        else None
    )
    return InstrumentDescriptorV1(
        instrument_kind=instrument_kind,
        contract_value=(
            ContractValueDescriptorV1(amount=amount, unit=unit)
            if amount is not None or unit is not None
            else None
        ),
        settlement=(
            SettlementDescriptorV1(asset_relationship=settlement)
            if settlement is not None
            else None
        ),
    )


def assess_capability(
    provider_id: str,
    datapoint: DataPointKind | DataPointDefinitionV1,
    descriptor: InstrumentDescriptorV1 | InstrumentReferenceV1 | Instrument,
    *,
    native_identities: Iterable[NativeIdentityV1] = (),
    temporal_mode: TemporalMode | None = None,
    market_observations: Iterable[str] | None = None,
    runtime_features: Iterable[str] = (),
    manifests: Sequence[AdapterManifest] | None = None,
) -> CapabilityAssessment:
    """Resolve acquisition support from CDM economics and runtime evidence."""

    provider = provider_id.strip().upper()
    if isinstance(datapoint, DataPointDefinitionV1):
        target_definition = datapoint
        target_kind = datapoint.kind
        target_temporal = datapoint.temporal_mode
    else:
        target_definition = None
        target_kind = datapoint
        target_temporal = temporal_mode
    if not isinstance(target_kind, DataPointKind):
        raise TypeError(
            "datapoint must be a CDM DataPointKind or DataPointDefinitionV1"
        )
    if target_temporal is not None and not isinstance(target_temporal, TemporalMode):
        raise TypeError("temporal_mode must be a CDM TemporalMode")

    if isinstance(descriptor, Instrument):
        neutral = instrument_descriptor_from_instrument(descriptor)
        identities = legacy_native_identities(descriptor)
    elif isinstance(descriptor, InstrumentReferenceV1):
        neutral = descriptor.descriptor
        identities = descriptor.native_identities
    elif isinstance(descriptor, InstrumentDescriptorV1):
        neutral = descriptor
        identities = tuple(native_identities)
    else:
        raise TypeError(
            "descriptor must be a CDM InstrumentDescriptorV1, "
            "InstrumentReferenceV1, or legacy Instrument"
        )

    available = tuple(manifests) if manifests is not None else builtin_manifests()
    manifest = next((item for item in available if item.provider_id == provider), None)
    if manifest is None:
        return CapabilityAssessment(
            provider,
            target_kind,
            target_temporal,
            CapabilityStatus.ADAPTER_UNAVAILABLE,
        )

    observed = (
        frozenset(market_observations) if market_observations is not None else None
    )
    features = frozenset(runtime_features)
    alternatives: list[CapabilityAlternative] = []
    for mapping in manifest.mappings:
        scenario_issues, contradicted = _scenario_issues(
            neutral, mapping.instrument_scenario
        )
        if contradicted:
            continue
        for capability in mapping.capabilities:
            if capability.datapoint.kind is not target_kind:
                continue
            if (
                target_definition is not None
                and capability.datapoint != target_definition
            ):
                continue
            if (
                target_temporal is not None
                and capability.datapoint.temporal_mode is not target_temporal
            ):
                continue
            issues = list(scenario_issues)
            issues.extend(
                _requirement_issues(
                    neutral,
                    identities,
                    capability.requirements,
                    observed,
                    features,
                )
            )
            if capability.declared_state is DeclaredState.UNAVAILABLE:
                issues.append(
                    CapabilityIssue(
                        "declared_unavailable",
                        "$.capability",
                        "the adapter explicitly declares this datapoint unavailable",
                    )
                )
            alternatives.append(
                CapabilityAlternative(
                    mapping.mapping_id,
                    capability.capability_id,
                    capability.declared_state,
                    tuple(_unique_issues(issues)),
                )
            )

    if not alternatives:
        return CapabilityAssessment(
            provider,
            target_kind,
            target_temporal,
            CapabilityStatus.UNSUPPORTED,
        )
    complete = [
        item
        for item in alternatives
        if not item.issues and item.declared_state is not DeclaredState.UNAVAILABLE
    ]
    if complete:
        status = CapabilityStatus.SUPPORTED
        common: tuple[CapabilityIssue, ...] = ()
    else:
        viable = [
            item
            for item in alternatives
            if not any(issue.code == "declared_unavailable" for issue in item.issues)
        ]
        if not viable:
            status = CapabilityStatus.UNSUPPORTED
            common = _common_issues(alternatives)
        else:
            common = _common_issues(viable)
            if not common:
                common = min(
                    viable,
                    key=lambda item: (len(item.issues), item.mapping_id),
                ).issues
            status = (
                CapabilityStatus.RUNTIME_UNAVAILABLE
                if common
                and all(issue.code == "missing_runtime_feature" for issue in common)
                else CapabilityStatus.METADATA_INCOMPLETE
            )
    return CapabilityAssessment(
        provider,
        target_kind,
        target_temporal,
        status,
        common,
        tuple(alternatives),
    )


def builtin_manifests() -> tuple[AdapterManifest, ...]:
    from perp_md.adapters.manifests import BUILTIN_ADAPTER_MANIFESTS

    return BUILTIN_ADAPTER_MANIFESTS


def planned_retrieval(
    assessment: CapabilityAssessment,
    *,
    fixed_interval_seconds: int | None = None,
    max_lookback_seconds: int | None = None,
    requires_explicit_start: bool = False,
    manifests: Sequence[AdapterManifest] | None = None,
) -> AcquisitionPlan:
    """Build a plan only when every supported alternative agrees on retrieval."""

    if assessment.status is not CapabilityStatus.SUPPORTED:
        return AcquisitionPlan(assessment)
    available = tuple(manifests) if manifests is not None else builtin_manifests()
    declarations = {
        (mapping.mapping_id, capability.capability_id): capability
        for manifest in available
        if manifest.provider_id == assessment.provider_id
        for mapping in manifest.mappings
        for capability in mapping.capabilities
    }
    supported = [
        declarations[(alternative.mapping_id, alternative.capability_id)]
        for alternative in assessment.alternatives
        if not alternative.issues
        and alternative.declared_state is not DeclaredState.UNAVAILABLE
        and (alternative.mapping_id, alternative.capability_id) in declarations
    ]
    retrievals = {item.retrieval for item in supported}
    if len(retrievals) != 1:
        return AcquisitionPlan(assessment)
    declaration = retrievals.pop()
    is_history = declaration.history_scope is not HistoryScope.NONE
    return AcquisitionPlan(
        assessment,
        PlannedRetrieval(
            declaration.request_scope,
            declaration.history_scope,
            declaration.pagination,
            fixed_interval_seconds if is_history else None,
            max_lookback_seconds if is_history else None,
            requires_explicit_start if is_history else False,
        ),
    )


def coverage_manifest(
    manifests: Sequence[AdapterManifest] | None = None,
    *,
    generated_at: datetime | str = MANIFEST_DECLARED_AT,
    producer_version: str | None = None,
) -> dict[str, Any]:
    """Export the deterministic, producer-owned declared coverage manifest."""

    values = tuple(manifests) if manifests is not None else builtin_manifests()
    timestamp = _rfc3339(generated_at)
    if producer_version is None:
        producer_version = PACKAGE_VERSION
    mappings = sorted(
        (mapping for manifest in values for mapping in manifest.mappings),
        key=lambda item: item.mapping_id,
    )
    return {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "manifest_id": MANIFEST_ID,
        "generated_at": timestamp,
        "producer": {"name": "perp-md", "version": producer_version},
        "mappings": [_mapping_data(item) for item in mappings],
    }


def coverage_manifest_json(
    manifests: Sequence[AdapterManifest] | None = None,
    *,
    generated_at: datetime | str = MANIFEST_DECLARED_AT,
    producer_version: str | None = None,
    indent: int | None = 2,
) -> str:
    return json.dumps(
        coverage_manifest(
            manifests,
            generated_at=generated_at,
            producer_version=producer_version,
        ),
        indent=indent,
        sort_keys=True,
        ensure_ascii=False,
    )


def load_coverage_schema() -> dict[str, Any]:
    resource = files("perp_md").joinpath("schemas/declared-coverage-v1.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def main() -> None:
    print(coverage_manifest_json())


def _mapping_data(mapping: NativeProductMapping) -> dict[str, Any]:
    return {
        "mapping_id": mapping.mapping_id,
        "adapter_id": mapping.adapter_id,
        "venue_id": mapping.provider_id,
        "native_instrument_type": {
            "family_id": mapping.family_id,
            "names": [
                {"name": item.name, "context": item.context}
                for item in mapping.native_names
            ],
        },
        "instrument_scenario": to_data(mapping.instrument_scenario),
        "capabilities": [
            {
                "capability_id": item.capability_id,
                "datapoint": to_data(item.datapoint),
                "declared_state": item.declared_state.value,
                "lineage": to_data(item.lineage) if item.lineage is not None else None,
                "source_observations": [
                    {"name": source.name, "context": source.context}
                    for source in item.source_observations
                ],
                "requirements": {
                    "identity_selectors": [
                        to_data(selector)
                        for selector in item.requirements.identity_selectors
                    ],
                    "instrument_metadata": list(item.requirements.instrument_metadata),
                    "market_observations": list(item.requirements.market_observations),
                    "runtime_features": list(item.requirements.runtime_features),
                },
                "retrieval": {
                    "request_scope": item.retrieval.request_scope.value,
                    "history_scope": item.retrieval.history_scope.value,
                    "pagination": item.retrieval.pagination.value,
                },
                "limitations": list(item.limitations),
            }
            for item in sorted(
                mapping.capabilities, key=lambda value: value.capability_id
            )
        ],
    }


def _scenario_issues(
    descriptor: InstrumentDescriptorV1,
    scenario: InstrumentScenarioV1,
) -> tuple[tuple[CapabilityIssue, ...], bool]:
    fields = (
        ("$.instrument_kind", descriptor.instrument_kind, scenario.instrument_kind),
        (
            "$.contract_value.unit",
            descriptor.contract_value.unit if descriptor.contract_value else None,
            scenario.contract_value_unit,
        ),
        (
            "$.settlement.asset_relationship",
            descriptor.settlement.asset_relationship if descriptor.settlement else None,
            scenario.settlement_asset_relationship,
        ),
    )
    issues: list[CapabilityIssue] = []
    for path, actual, expected in fields:
        if actual is None:
            issues.append(
                CapabilityIssue(
                    "missing_economic_attribute",
                    path,
                    "the CDM scenario selection requires this economic attribute",
                )
            )
        elif actual is not expected:
            return (), True
    return tuple(issues), False


def _requirement_issues(
    descriptor: InstrumentDescriptorV1,
    identities: tuple[NativeIdentityV1, ...],
    requirements: CapabilityRequirements,
    observations: frozenset[str] | None,
    features: frozenset[str],
) -> list[CapabilityIssue]:
    issues: list[CapabilityIssue] = []
    for selector in requirements.identity_selectors:
        selection = select_native_identity(identities, selector)
        path = _identity_path(selector)
        if selection.status is NativeIdentitySelectionStatus.MISSING:
            issues.append(
                CapabilityIssue(
                    "missing_native_identity",
                    path,
                    "the provider operation requires one exact native identity",
                    selector,
                )
            )
        elif selection.status is NativeIdentitySelectionStatus.AMBIGUOUS:
            issues.append(
                CapabilityIssue(
                    "ambiguous_native_identity",
                    path,
                    "the provider operation requires an unambiguous native identity",
                    selector,
                )
            )
    for path in requirements.instrument_metadata:
        if _descriptor_value(descriptor, path) is None:
            issues.append(
                CapabilityIssue(
                    "missing_economic_attribute",
                    path,
                    "the declared acquisition or conversion requires this CDM attribute",
                )
            )
    for name in requirements.market_observations:
        if observations is not None and name not in observations:
            issues.append(
                CapabilityIssue(
                    "missing_market_observation",
                    f"$.market_observations.{name}",
                    "the declared conversion requires this market observation",
                )
            )
    for name in requirements.runtime_features:
        if name not in features:
            issues.append(
                CapabilityIssue(
                    "missing_runtime_feature",
                    f"$.runtime.features.{name}",
                    "the conditional adapter requires this runtime feature",
                )
            )
    return issues


def _identity_path(selector: NativeIdentitySelectorV1) -> str:
    return (
        "$.native_identities"
        f"[role={selector.role.value},namespace={selector.namespace}]"
    )


def _descriptor_value(value: InstrumentDescriptorV1, path: str) -> object:
    names = {
        "$.instrument_kind": value.instrument_kind,
        "$.contract_value.amount": (
            value.contract_value.amount if value.contract_value else None
        ),
        "$.contract_value.unit": (
            value.contract_value.unit if value.contract_value else None
        ),
        "$.settlement.asset_relationship": (
            value.settlement.asset_relationship if value.settlement else None
        ),
        "$.funding.mechanism": value.funding.mechanism if value.funding else None,
        "$.funding.interval_seconds": (
            value.funding.interval_seconds if value.funding else None
        ),
    }
    if path not in names:
        raise ValueError(f"unsupported CDM descriptor path: {path}")
    return names[path]


def _unique_issues(issues: Iterable[CapabilityIssue]) -> tuple[CapabilityIssue, ...]:
    return tuple(
        {(item.code, item.path, item.message): item for item in issues}.values()
    )


def _common_issues(
    alternatives: Sequence[CapabilityAlternative],
) -> tuple[CapabilityIssue, ...]:
    if not alternatives:
        return ()
    common = {(item.code, item.path) for item in alternatives[0].issues}
    for alternative in alternatives[1:]:
        common.intersection_update(
            (item.code, item.path) for item in alternative.issues
        )
    return tuple(
        item for item in alternatives[0].issues if (item.code, item.path) in common
    )


def _rfc3339(value: datetime | str) -> str:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError("generated_at must be a datetime or RFC3339 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
