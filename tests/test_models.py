from __future__ import annotations

import pytest

from perp_md import (
    ContractDirection,
    HistoryRange,
    Instrument,
    InvalidInstrument,
    InvalidResponse,
    OpenInterestObservation,
    find_resume_time,
)


def test_instrument_normalizes_venue_and_preserves_native_symbol():
    instrument = Instrument(" venue ", "Base_Quote", contract_multiplier=1)
    assert instrument.venue == "VENUE"
    assert instrument.symbol == "Base_Quote"


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), "invalid"])
def test_contract_multiplier_must_be_positive_and_finite(value):
    with pytest.raises(InvalidInstrument):
        Instrument("VENUE", "BASE-QUOTE", contract_multiplier=value)


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_observation_rejects_invalid_notional(value):
    with pytest.raises(InvalidResponse):
        OpenInterestObservation(1, value)


def test_zero_open_interest_is_valid():
    assert OpenInterestObservation(1, 0).value_usd == 0


def test_history_range_is_bounded_and_ordered():
    assert HistoryRange(1, 2).end_ms == 2
    with pytest.raises(ValueError):
        HistoryRange(2, 1)


def test_resume_time_finds_leading_and_internal_gaps():
    assert find_resume_time([], floor_ms=0, interval_ms=300) == 0
    assert find_resume_time([300, 600], floor_ms=0, interval_ms=300) == 600
    assert find_resume_time([0, 300, 900], floor_ms=0, interval_ms=300) == 300


def test_contract_direction_is_explicit():
    instrument = Instrument(
        "VENUE", "BASE-QUOTE", contract_direction=ContractDirection.LINEAR
    )
    assert instrument.contract_direction is ContractDirection.LINEAR
