from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from cdm import OpenInterestMeasure, OpenInterestValueV1

from perp_md.errors import InvalidInstrument, InvalidResponse
from perp_md.models import (
    ContractDirection,
    Instrument,
    NativeUnit,
)


def number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidResponse("venue returned a non-numeric value") from exc
    if not math.isfinite(result):
        raise InvalidResponse("venue returned a non-finite value")
    return result


def contract_value_usd(
    instrument: Instrument,
    contracts: float,
    mark_price: float | None,
) -> float:
    if instrument.contract_multiplier is None:
        raise InvalidInstrument(
            "contract_multiplier is required for contract-count open interest"
        )
    if instrument.contract_direction is ContractDirection.LINEAR:
        if mark_price is None or number(mark_price) <= 0:
            raise InvalidResponse(
                "positive mark price is required for linear open-interest conversion"
            )
        return number(contracts) * instrument.contract_multiplier * number(mark_price)
    if instrument.contract_direction is ContractDirection.INVERSE:
        return number(contracts) * instrument.contract_multiplier
    raise InvalidInstrument(
        "contract_direction is required for contract-count open interest"
    )


def verify_multiplier(instrument: Instrument, venue_value: Any) -> None:
    if venue_value in (None, ""):
        raise InvalidResponse("venue omitted its contract multiplier")
    if instrument.contract_multiplier is None:
        raise InvalidInstrument(
            "contract_multiplier is required for contract-count open interest"
        )
    if not math.isclose(
        instrument.contract_multiplier,
        number(venue_value),
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise InvalidInstrument(
            "contract_multiplier disagrees with venue contract metadata"
        )


def proven_base_quantity(
    instrument: Instrument,
    native_value: float,
    native_unit: NativeUnit,
) -> OpenInterestValueV1 | None:
    """Return canonical base quantity only when the supplied metadata proves it."""
    native = number(native_value)
    if native < 0:
        raise InvalidResponse("native open interest must be non-negative")
    if native_unit is NativeUnit.BASE:
        return OpenInterestValueV1(
            Decimal(str(native)), OpenInterestMeasure.BASE_QUANTITY
        )
    if (
        native_unit is NativeUnit.CONTRACTS
        and instrument.contract_direction is ContractDirection.LINEAR
        and instrument.contract_multiplier is not None
    ):
        return OpenInterestValueV1(
            Decimal(str(native * instrument.contract_multiplier)),
            OpenInterestMeasure.BASE_QUANTITY,
        )
    return None
