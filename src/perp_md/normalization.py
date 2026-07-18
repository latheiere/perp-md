from __future__ import annotations

import math
from typing import Any

from perp_md.errors import InvalidInstrument, InvalidResponse
from perp_md.models import ContractDirection, Instrument


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
        raise InvalidInstrument("contract_multiplier is required for contract-count open interest")
    if instrument.contract_direction is ContractDirection.LINEAR:
        if mark_price is None or number(mark_price) <= 0:
            raise InvalidResponse("positive mark price is required for linear open-interest conversion")
        return number(contracts) * instrument.contract_multiplier * number(mark_price)
    if instrument.contract_direction is ContractDirection.INVERSE:
        return number(contracts) * instrument.contract_multiplier
    raise InvalidInstrument("contract_direction is required for contract-count open interest")


def verify_multiplier(instrument: Instrument, venue_value: Any) -> None:
    if venue_value in (None, ""):
        raise InvalidResponse("venue omitted its contract multiplier")
    if instrument.contract_multiplier is None:
        raise InvalidInstrument("contract_multiplier is required for contract-count open interest")
    if not math.isclose(
        instrument.contract_multiplier,
        number(venue_value),
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise InvalidInstrument("contract_multiplier disagrees with venue contract metadata")
