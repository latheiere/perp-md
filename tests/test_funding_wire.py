from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from cdm import (
    DataPointDefinitionV1,
    DataPointKind,
    DerivationKind,
    DerivationStepV1,
    FundingIntervalKind,
    FundingIntervalV1,
    FundingRateKind,
    FundingSampleV1,
    MeasurementLineageV1,
    MeasurementUnit,
    TemporalMode,
    load_schemas,
)
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from perp_md import (
    FUNDING_OBSERVATION_SCHEMA_ID,
    FundingObservation,
    FundingObservationDecodeError,
    ProviderFundingEvidence,
    funding_observation_from_data,
    funding_observation_from_json,
    funding_observation_to_data,
    funding_observation_to_json,
    load_funding_observation_schema,
)


def observation() -> FundingObservation:
    effective_at = datetime(2026, 8, 10, 8, tzinfo=timezone.utc)
    window_start = datetime(2026, 8, 10, 4, tzinfo=timezone.utc)
    sample = FundingSampleV1(
        Decimal("-0.00025"),
        FundingRateKind.SETTLED,
        None,
        effective_at,
        FundingIntervalV1(
            FundingIntervalKind.OBSERVED_WINDOW,
            duration_seconds=14_400,
            window_start=window_start,
        ),
        MeasurementLineageV1(
            DataPointDefinitionV1(
                DataPointKind.FUNDING_SETTLED_RATE,
                TemporalMode.HISTORICAL,
                MeasurementUnit.RATE_FRACTION,
            ),
            (
                DerivationStepV1(DerivationKind.NATIVE_REPORTED),
                DerivationStepV1(
                    DerivationKind.PROVIDER_FORMULA,
                    "perp_md.funding.absolute_to_relative.linear.v1",
                ),
            ),
        ),
    )
    return FundingObservation(
        sample,
        ProviderFundingEvidence(
            "funding.absolute_amount",
            datetime(2026, 8, 10, 8, 0, 1, 123000, tzinfo=timezone.utc),
            Decimal("-1.25"),
            Decimal("5000"),
        ),
    )


def validator() -> Draft202012Validator:
    schema = load_funding_observation_schema()
    schemas = [schema, *load_schemas().values()]
    registry = Registry().with_resources(
        (
            str(item["$id"]),
            Resource.from_contents(item),
        )
        for item in schemas
    )
    return Draft202012Validator(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    )


def test_acquisition_envelope_round_trips_embedded_sample_and_evidence_losslessly():
    original = observation()
    data = funding_observation_to_data(original)
    encoded = funding_observation_to_json(original)

    assert data["schema_id"] == FUNDING_OBSERVATION_SCHEMA_ID
    assert data["sample"]["interval"] == {
        "kind": "observed_window",
        "duration_seconds": 14_400,
        "window_start": "2026-08-10T04:00:00Z",
    }
    assert (
        data["sample"]["lineage"]
        == funding_observation_to_data(original)["sample"]["lineage"]
    )
    assert data["provider_evidence"] == {
        "source_observation": "funding.absolute_amount",
        "retrieved_at": "2026-08-10T08:00:01.123000Z",
        "source_value": "-1.25",
        "mark_price": "5000",
    }
    assert funding_observation_from_data(data) == original
    assert funding_observation_from_json(encoded) == original
    assert encoded == json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert "native_identities" not in encoded
    assert "provider_id" not in encoded


def test_producer_schema_validates_the_exact_codec_output_with_cdm_references():
    schema = load_funding_observation_schema()
    data = funding_observation_to_data(observation())

    assert schema["$id"] == FUNDING_OBSERVATION_SCHEMA_ID
    assert schema["additionalProperties"] is False
    validator().validate(data)


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        (lambda value: value.update({"provider_id": "UNDECLARED"}), "$.provider_id"),
        (
            lambda value: value["provider_evidence"].update(
                {"native_symbol": "UNDECLARED"}
            ),
            "$.provider_evidence.native_symbol",
        ),
        (
            lambda value: value["provider_evidence"].update(
                {"retrieved_at": "2026-08-10T08:00:01+00:00"}
            ),
            "$.provider_evidence.retrieved_at",
        ),
        (
            lambda value: value["provider_evidence"].update({"source_value": "-0"}),
            "$.provider_evidence.source_value",
        ),
        (
            lambda value: value["provider_evidence"].update({"mark_price": "0"}),
            "$.provider_evidence.mark_price",
        ),
    ],
)
def test_schema_and_decoder_reject_the_same_noncanonical_envelope_shapes(
    mutation,
    path: str,
):
    data = copy.deepcopy(funding_observation_to_data(observation()))
    mutation(data)

    with pytest.raises(FundingObservationDecodeError) as decoded:
        funding_observation_from_data(data)
    with pytest.raises(ValidationError):
        validator().validate(data)

    assert decoded.value.path == path


def test_embedded_cdm_decode_issue_is_reported_at_the_envelope_field_path():
    data = funding_observation_to_data(observation())
    data["sample"]["interval"]["duration_seconds"] = 0

    with pytest.raises(FundingObservationDecodeError) as raised:
        funding_observation_from_data(data)

    assert raised.value.code == "invalid_value"
    assert raised.value.path == "$.sample.interval.duration_seconds"


def test_json_decoder_rejects_invalid_input_without_leaking_json_errors():
    with pytest.raises(FundingObservationDecodeError) as raised:
        funding_observation_from_json("{")

    assert raised.value.code == "invalid_json"
    assert raised.value.path == "$"
