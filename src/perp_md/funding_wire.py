"""Strict wire codec for the acquisition-owned funding observation envelope."""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from typing import Any, cast

from cdm import DecodeError, FundingSampleV1, from_data, to_data

from perp_md.errors import FundingObservationDecodeError
from perp_md.models import FundingObservation, ProviderFundingEvidence

FUNDING_OBSERVATION_SCHEMA_ID = "urn:perp-md:schema:funding-observation:1"
_SCHEMA_RESOURCE = "funding-observation-v1.schema.json"
_DECIMAL_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$")


def funding_observation_to_data(
    observation: FundingObservation,
) -> dict[str, object]:
    """Encode a funding observation into its versioned JSON-compatible object."""

    if not isinstance(observation, FundingObservation):
        raise TypeError("observation must be a FundingObservation")
    evidence = observation.provider_evidence
    provider_evidence: dict[str, object] = {
        "source_observation": evidence.source_observation,
        "retrieved_at": _timestamp_text(evidence.retrieved_at),
    }
    if evidence.source_value is not None:
        provider_evidence["source_value"] = _decimal_text(evidence.source_value)
    if evidence.mark_price is not None:
        provider_evidence["mark_price"] = _decimal_text(evidence.mark_price)
    return {
        "schema_id": FUNDING_OBSERVATION_SCHEMA_ID,
        "sample": to_data(observation.sample),
        "provider_evidence": provider_evidence,
    }


def funding_observation_from_data(value: object) -> FundingObservation:
    """Decode a funding observation after strict shape and canonical checks."""

    data = _object(value, "$")
    _fields(
        data,
        required={"schema_id", "sample", "provider_evidence"},
        optional=set(),
        path="$",
    )
    if data["schema_id"] != FUNDING_OBSERVATION_SCHEMA_ID:
        raise FundingObservationDecodeError(
            "unsupported_schema",
            "$.schema_id",
            f"schema identifier {data['schema_id']!r} is not supported",
        )
    try:
        sample = from_data(data["sample"])
    except DecodeError as error:
        raise FundingObservationDecodeError(
            error.code,
            _nested_path("$.sample", error.path),
            error.message,
        ) from error
    if not isinstance(sample, FundingSampleV1):
        raise FundingObservationDecodeError(
            "invalid_type",
            "$.sample.schema_id",
            "expected a CDM funding sample",
        )

    evidence_data = _object(data["provider_evidence"], "$.provider_evidence")
    _fields(
        evidence_data,
        required={"source_observation", "retrieved_at"},
        optional={"source_value", "mark_price"},
        path="$.provider_evidence",
    )
    source_observation = evidence_data["source_observation"]
    if (
        not isinstance(source_observation, str)
        or not source_observation
        or source_observation != source_observation.strip()
    ):
        raise FundingObservationDecodeError(
            "invalid_value",
            "$.provider_evidence.source_observation",
            "expected a non-empty string without surrounding whitespace",
        )
    retrieved_at = _timestamp(
        evidence_data["retrieved_at"],
        "$.provider_evidence.retrieved_at",
    )
    source_value = (
        _decimal(
            evidence_data["source_value"],
            "$.provider_evidence.source_value",
        )
        if "source_value" in evidence_data
        else None
    )
    mark_price = (
        _decimal(
            evidence_data["mark_price"],
            "$.provider_evidence.mark_price",
            positive=True,
        )
        if "mark_price" in evidence_data
        else None
    )
    return FundingObservation(
        sample,
        ProviderFundingEvidence(
            source_observation,
            retrieved_at,
            source_value,
            mark_price,
        ),
    )


def funding_observation_to_json(observation: FundingObservation) -> str:
    """Encode a funding observation as deterministic compact JSON."""

    return json.dumps(
        funding_observation_to_data(observation),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def funding_observation_from_json(
    value: str | bytes | bytearray,
) -> FundingObservation:
    """Decode JSON into a typed funding observation."""

    try:
        data: Any = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
        raise FundingObservationDecodeError(
            "invalid_json",
            "$",
            "input is not valid JSON",
        ) from error
    return funding_observation_from_data(data)


def load_funding_observation_schema() -> dict[str, object]:
    """Load a fresh copy of the producer-owned funding observation schema."""

    resource = files("perp_md").joinpath("schemas", _SCHEMA_RESOURCE)
    value = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(value, dict):  # pragma: no cover - package integrity guard
        raise RuntimeError("funding observation schema is not an object")
    return cast(dict[str, object], value)


def _object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FundingObservationDecodeError(
            "invalid_type",
            path,
            "expected an object with string keys",
        )
    return cast(dict[str, object], value)


def _fields(
    value: dict[str, object],
    *,
    required: set[str],
    optional: set[str],
    path: str,
) -> None:
    missing = sorted(required.difference(value))
    if missing:
        raise FundingObservationDecodeError(
            "missing_field",
            f"{path}.{missing[0]}",
            "required field is missing",
        )
    unknown = sorted(set(value).difference(required | optional))
    if unknown:
        raise FundingObservationDecodeError(
            "unknown_field",
            f"{path}.{unknown[0]}",
            "field is not defined",
        )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _decimal(value: object, path: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL_PATTERN.fullmatch(value):
        raise FundingObservationDecodeError(
            "invalid_decimal",
            path,
            "expected a canonical decimal string",
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:  # pragma: no cover - guarded by pattern
        raise FundingObservationDecodeError(
            "invalid_decimal",
            path,
            "expected a canonical decimal string",
        ) from error
    if _decimal_text(parsed) != value or (positive and parsed <= 0):
        raise FundingObservationDecodeError(
            "invalid_decimal",
            path,
            "expected a positive canonical decimal string"
            if positive
            else "expected a canonical decimal string",
        )
    return parsed


def _timestamp_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _timestamp(value: object, path: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise FundingObservationDecodeError(
            "invalid_timestamp",
            path,
            "expected a canonical RFC 3339 UTC timestamp",
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise FundingObservationDecodeError(
            "invalid_timestamp",
            path,
            "expected a canonical RFC 3339 UTC timestamp",
        ) from error
    if _timestamp_text(parsed) != value:
        raise FundingObservationDecodeError(
            "invalid_timestamp",
            path,
            "expected a canonical RFC 3339 UTC timestamp",
        )
    return parsed


def _nested_path(prefix: str, path: str) -> str:
    if path == "$":
        return prefix
    if path.startswith("$."):
        return prefix + path[1:]
    return prefix
