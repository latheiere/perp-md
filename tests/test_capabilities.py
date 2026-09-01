from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from cdm import (
    AssetRelationship,
    ContractValueDescriptorV1,
    ContractValueUnit,
    DataPointDefinitionV1,
    DataPointKind,
    InstrumentDescriptorV1,
    InstrumentKind,
    InstrumentScenarioV1,
    MeasurementLineageV1,
    MeasurementUnit,
    NativeIdentityNamespace,
    NativeIdentityRole,
    NativeIdentitySelectorV1,
    NativeIdentityV1,
    NotionalDenomination,
    SettlementDescriptorV1,
    TemporalMode,
    from_data,
    instrument_scenario,
    load_schemas,
    to_data,
)
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from perp_md import (
    CapabilityStatus,
    ContractDirection,
    Instrument,
    assess_capability,
    coverage_manifest,
    coverage_manifest_json,
    instrument_descriptor_from_instrument,
    load_coverage_schema,
)
from perp_md.capabilities import (
    CCXT_FUNDING_FEATURE,
    CCXT_FUNDING_HISTORY_FEATURE,
)

FIXTURES = Path(__file__).parent / "fixtures"


def descriptor(
    direction: str | None = None,
    *,
    amount: str | None = None,
    kind: InstrumentKind | None = InstrumentKind.PERPETUAL_SWAP,
) -> InstrumentDescriptorV1:
    unit = (
        ContractValueUnit.BASE
        if direction == "linear"
        else ContractValueUnit.QUOTE
        if direction == "inverse"
        else None
    )
    relationship = (
        AssetRelationship.QUOTE
        if direction == "linear"
        else AssetRelationship.BASE
        if direction == "inverse"
        else None
    )
    return InstrumentDescriptorV1(
        instrument_kind=kind,
        contract_value=(
            ContractValueDescriptorV1(
                amount=Decimal(amount) if amount is not None else None,
                unit=unit,
            )
            if unit is not None or amount is not None
            else None
        ),
        settlement=(
            SettlementDescriptorV1(asset_relationship=relationship)
            if relationship is not None
            else None
        ),
    )


def identity(
    role: NativeIdentityRole = NativeIdentityRole.INSTRUMENT,
    namespace: str = "rest",
    value: str = "NATIVE-ID",
) -> tuple[NativeIdentityV1, ...]:
    return (NativeIdentityV1(role, NativeIdentityNamespace(namespace), value),)


def test_incomplete_descriptor_reports_neutral_scenario_requirements():
    assessment = assess_capability(
        "KRAKEN",
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        descriptor(),
        native_identities=identity(),
        temporal_mode=TemporalMode.CURRENT,
    )

    assert assessment.status is CapabilityStatus.METADATA_INCOMPLETE
    assert assessment.missing_fields == (
        "$.contract_value.unit",
        "$.settlement.asset_relationship",
    )
    assert {item.mapping_id for item in assessment.alternatives} == {
        "kraken.flexible-futures.linear.perpetual.v1",
        "kraken.futures-inverse.inverse.perpetual.v1",
    }


def test_selected_scenario_reports_only_its_missing_conversion_metadata():
    assessment = assess_capability(
        "KRAKEN",
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        descriptor("inverse"),
        native_identities=identity(),
        temporal_mode=TemporalMode.CURRENT,
    )

    assert assessment.status is CapabilityStatus.METADATA_INCOMPLETE
    assert assessment.missing_fields == ("$.contract_value.amount",)
    assert len(assessment.alternatives) == 1


def test_temporal_target_selects_distinct_endpoint_identity_requirements():
    neutral = descriptor("inverse", amount="10")
    current = assess_capability(
        "BINANCE",
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        neutral,
        native_identities=identity(),
        temporal_mode=TemporalMode.CURRENT,
    )
    history = assess_capability(
        "BINANCE",
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        neutral,
        native_identities=identity(),
        temporal_mode=TemporalMode.HISTORICAL,
    )

    assert current.status is CapabilityStatus.SUPPORTED
    assert history.status is CapabilityStatus.METADATA_INCOMPLETE
    assert history.issues[0].identity_selector == NativeIdentitySelectorV1(
        NativeIdentityRole.PAIR,
        NativeIdentityNamespace.REST,
    )


@pytest.mark.parametrize(
    ("provider", "runtime_features"),
    [
        ("BITFINEX", ()),
        ("COINBASE", ("ccxt.open_interest.specialized_catalog",)),
    ],
)
def test_specialized_endpoint_identity_is_declared_separately(
    provider: str,
    runtime_features: tuple[str, ...],
):
    neutral = descriptor("linear", amount="1")
    incomplete = assess_capability(
        provider,
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        neutral,
        native_identities=identity(),
        temporal_mode=TemporalMode.CURRENT,
        runtime_features=runtime_features,
    )
    complete = assess_capability(
        provider,
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        neutral,
        native_identities=identity(
            NativeIdentityRole.INSTRUMENT,
            "rest:derivative-status"
            if provider == "BITFINEX"
            else "rest:instrument-catalog",
            "EXACT-ENDPOINT-ID",
        ),
        temporal_mode=TemporalMode.CURRENT,
        runtime_features=runtime_features,
    )

    assert incomplete.status is CapabilityStatus.METADATA_INCOMPLETE
    assert incomplete.issues[0].code == "missing_native_identity"
    assert incomplete.issues[0].identity_selector is not None
    assert complete.status is CapabilityStatus.SUPPORTED


def test_provider_reported_base_quantity_and_notional_need_no_contract_metadata():
    neutral = descriptor("linear")
    notional = assess_capability(
        "BYBIT",
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        neutral,
        native_identities=identity(),
        temporal_mode=TemporalMode.CURRENT,
    )
    base = assess_capability(
        "BYBIT",
        DataPointKind.OPEN_INTEREST_BASE_QUANTITY,
        neutral,
        native_identities=identity(),
        temporal_mode=TemporalMode.CURRENT,
    )

    assert notional.status is CapabilityStatus.SUPPORTED
    assert base.status is CapabilityStatus.SUPPORTED


def test_exact_datapoint_definition_does_not_match_another_denomination():
    assessment = assess_capability(
        "BYBIT",
        DataPointDefinitionV1(
            kind=DataPointKind.OPEN_INTEREST_NOTIONAL,
            temporal_mode=TemporalMode.CURRENT,
            unit=MeasurementUnit.NOTIONAL,
            denomination=NotionalDenomination.QUOTE,
        ),
        descriptor("linear"),
        native_identities=identity(),
    )

    assert assessment.status is CapabilityStatus.UNSUPPORTED


def test_runtime_can_assess_declared_supporting_observation_availability():
    assessment = assess_capability(
        "KRAKEN",
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        descriptor("linear"),
        native_identities=identity(),
        temporal_mode=TemporalMode.CURRENT,
        market_observations=(),
    )

    assert assessment.status is CapabilityStatus.METADATA_INCOMPLETE
    assert assessment.missing_fields == ("$.market_observations.mark_price",)


def test_conditional_adapter_requires_explicit_runtime_evidence():
    neutral = descriptor("linear", amount="1")
    unavailable = assess_capability(
        "DERIBIT",
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        neutral,
        native_identities=identity(),
        temporal_mode=TemporalMode.CURRENT,
    )
    available = assess_capability(
        "DERIBIT",
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        neutral,
        native_identities=identity(),
        temporal_mode=TemporalMode.CURRENT,
        runtime_features=("ccxt.fetch_open_interest",),
    )

    assert unavailable.status is CapabilityStatus.RUNTIME_UNAVAILABLE
    assert unavailable.missing_fields == (
        "$.runtime.features.ccxt.fetch_open_interest",
    )
    assert available.status is CapabilityStatus.SUPPORTED


@pytest.mark.parametrize(
    ("provider", "runtime_features"),
    [
        ("BINANCE", ()),
        ("OKX", ()),
        ("WHITEBIT", ("ccxt.fetch_funding_rate",)),
    ],
)
def test_current_funding_interval_capability_tracks_source_evidence(
    provider: str, runtime_features: tuple[str, ...]
):
    assessment = assess_capability(
        provider,
        DataPointKind.FUNDING_INTERVAL,
        descriptor("linear"),
        native_identities=identity(),
        temporal_mode=TemporalMode.CURRENT,
        runtime_features=runtime_features,
    )

    assert assessment.status is CapabilityStatus.SUPPORTED


def test_documentation_insufficient_open_interest_is_absent_while_funding_remains_declared():
    mappings = [
        mapping
        for mapping in coverage_manifest()["mappings"]
        if mapping["venue_id"] == "WHITEBIT"
    ]
    kinds = {
        capability["datapoint"]["kind"]
        for mapping in mappings
        for capability in mapping["capabilities"]
    }

    assert not any(kind.startswith("open_interest.") for kind in kinds)
    assert "funding.indicative_rate" in kinds
    assert "funding.settled_rate" in kinds


def test_native_notional_and_funding_share_one_exact_product_mapping():
    mapping = next(
        mapping
        for mapping in coverage_manifest()["mappings"]
        if mapping["mapping_id"] == "xt.perpetual.linear.perpetual.v1"
    )
    capabilities = mapping["capabilities"]

    assert mapping["adapter_id"] == "native.xt"
    assert {item["datapoint"]["kind"] for item in capabilities} == {
        "open_interest.notional",
        "funding.next_rate",
        "funding.settled_rate",
        "funding.interval",
    }
    oi = next(
        item
        for item in capabilities
        if item["datapoint"]["kind"] == "open_interest.notional"
    )
    assert oi["datapoint"]["temporal_mode"] == "current"

    subject = descriptor("linear")
    native_oi = assess_capability(
        "XT",
        DataPointKind.OPEN_INTEREST_NOTIONAL,
        subject,
        native_identities=identity(),
        temporal_mode=TemporalMode.CURRENT,
    )
    current_funding = assess_capability(
        "XT",
        DataPointKind.FUNDING_NEXT_RATE,
        subject,
        native_identities=identity(),
        temporal_mode=TemporalMode.NEXT,
    )
    historical_funding = assess_capability(
        "XT",
        DataPointKind.FUNDING_SETTLED_RATE,
        subject,
        native_identities=identity(),
        temporal_mode=TemporalMode.HISTORICAL,
    )

    assert native_oi.status is CapabilityStatus.SUPPORTED
    assert current_funding.status is CapabilityStatus.SUPPORTED
    assert historical_funding.status is CapabilityStatus.SUPPORTED


@pytest.mark.parametrize(
    ("mapping_id", "adapter_id", "expected_funding", "history_pagination"),
    [
        (
            "mexc.perp.linear.perpetual.v1",
            "native.mexc",
            {"funding.next_rate", "funding.settled_rate", "funding.interval"},
            "page_number",
        ),
        (
            "xt.perpetual.linear.perpetual.v1",
            "native.xt",
            {"funding.next_rate", "funding.settled_rate", "funding.interval"},
            "time_cursor",
        ),
        (
            "bitfinex.f0.linear.perpetual.v1",
            "native.bitfinex",
            {"funding.indicative_rate", "funding.interval"},
            None,
        ),
    ],
)
def test_native_funding_manifests_declare_only_proven_temporal_products(
    mapping_id, adapter_id, expected_funding, history_pagination
):
    mapping = next(
        item
        for item in coverage_manifest()["mappings"]
        if item["mapping_id"] == mapping_id
    )
    funding = [
        item
        for item in mapping["capabilities"]
        if item["datapoint"]["kind"].startswith("funding.")
    ]

    assert mapping["adapter_id"] == adapter_id
    assert {item["datapoint"]["kind"] for item in funding} == expected_funding
    assert all(not item["requirements"]["runtime_features"] for item in funding)
    historical = [
        item
        for item in funding
        if item["datapoint"]["temporal_mode"] == "historical"
    ]
    assert [item["retrieval"]["pagination"] for item in historical] == (
        [history_pagination] if history_pagination is not None else []
    )


def test_ambiguous_standardized_amount_manifest_excludes_open_interest():
    mapping = next(
        item
        for item in coverage_manifest()["mappings"]
        if item["mapping_id"] == "weex.swap.linear.perpetual.v1"
    )
    capabilities = mapping["capabilities"]
    kinds = {item["datapoint"]["kind"] for item in capabilities}
    assert not {kind for kind in kinds if kind.startswith("open_interest.")}
    assert {
        kind for kind in kinds if kind.startswith("funding.")
    } == {
        "funding.indicative_rate",
        "funding.settled_rate",
        "funding.interval",
    }


def test_legacy_instrument_projection_is_a_compatibility_seam_into_cdm():
    projected = instrument_descriptor_from_instrument(
        Instrument(
            "VENUE",
            "NATIVE-ID",
            contract_direction=ContractDirection.LINEAR,
            contract_multiplier=1,
        )
    )

    assert projected.instrument_kind is InstrumentKind.PERPETUAL_SWAP
    assert projected.contract_value == ContractValueDescriptorV1(
        amount=Decimal("1.0"),
        unit=ContractValueUnit.BASE,
    )
    assert projected.settlement == SettlementDescriptorV1(
        asset_relationship=AssetRelationship.QUOTE
    )


def test_manifest_is_deterministic_and_embeds_exact_cdm_wire_contracts():
    first = coverage_manifest_json()
    second = coverage_manifest_json()
    payload = json.loads(first)

    assert first == second
    assert payload["schema_version"] == "acquisition.coverage/v1"
    assert payload["producer"] == {"name": "perp-md", "version": "0.5.0"}
    assert payload["mappings"] == sorted(
        payload["mappings"], key=lambda item: item["mapping_id"]
    )
    mapping = next(
        item for item in payload["mappings"] if item["venue_id"] == "BINANCE"
    )
    assert isinstance(from_data(mapping["instrument_scenario"]), InstrumentScenarioV1)
    capability = mapping["capabilities"][0]
    assert isinstance(from_data(capability["datapoint"]), DataPointDefinitionV1)
    assert isinstance(from_data(capability["lineage"]), MeasurementLineageV1)
    assert isinstance(
        from_data(capability["requirements"]["identity_selectors"][0]),
        NativeIdentitySelectorV1,
    )
    assert capability["lineage"]["output"] == capability["datapoint"]
    assert "$.provider." not in first
    assert json.loads(first) == coverage_manifest()


def test_producer_schema_has_stable_identity_and_closed_top_level_shape():
    schema = load_coverage_schema()

    assert schema["$id"] == "urn:perp-md:schema:declared-coverage:1"
    assert schema["properties"]["schema_version"]["const"] == "acquisition.coverage/v1"
    assert schema["additionalProperties"] is False


def test_producer_schema_validates_the_deterministic_manifest_with_cdm_references():
    schema = load_coverage_schema()
    schemas = [schema, *load_schemas().values()]
    registry = Registry().with_resources(
        (str(item["$id"]), Resource.from_contents(item)) for item in schemas
    )

    Draft202012Validator(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    ).validate(coverage_manifest())


def test_exact_contextual_native_evidence_produces_mutual_unique_catalog_joins():
    catalog_groups = json.loads(
        (FIXTURES / "catalog_product_family_evidence.json").read_text(encoding="utf-8")
    )
    mappings = coverage_manifest()["mappings"]

    expected_exact_mappings = {
        group["expected_mapping_id"]
        for group in catalog_groups
        if group["expected_mapping_id"] is not None
    }
    declared_exact_mappings = {
        mapping["mapping_id"]
        for mapping in mappings
        if all(
            name["context"] != "provider product family template"
            for name in mapping["native_instrument_type"]["names"]
        )
    }
    assert expected_exact_mappings == declared_exact_mappings

    for group in catalog_groups:
        scenario = to_data(
            instrument_scenario(
                descriptor(
                    group["direction"],
                    kind=(
                        InstrumentKind.FUTURE
                        if group.get("kind") == "future"
                        else InstrumentKind.PERPETUAL_SWAP
                    ),
                )
            )
        )
        catalog_evidence = {
            (item["name"].strip().casefold(), item["context"].strip().casefold())
            for item in group["names"]
        }
        candidates = []
        for mapping in mappings:
            mapping_evidence = {
                (
                    item["name"].strip().casefold(),
                    item["context"].strip().casefold(),
                )
                for item in mapping["native_instrument_type"]["names"]
            }
            if (
                mapping["venue_id"] == group["venue_id"]
                and mapping["instrument_scenario"] == scenario
                and mapping_evidence & catalog_evidence
            ):
                candidates.append(mapping["mapping_id"])

        expected = group["expected_mapping_id"]
        assert candidates == ([] if expected is None else [expected])
