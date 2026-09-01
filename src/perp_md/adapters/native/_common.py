from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from cdm import OpenInterestMeasure, OpenInterestValueV1

from perp_md.errors import (
    DataUnavailable,
    InvalidInstrument,
    InvalidResponse,
    PaginationError,
)
from perp_md.identity import (
    REST_DERIVATIVE_STATUS_INSTRUMENT,
    REST_PAIR,
    REST_PRODUCT_FAMILY,
    RPC_INSTRUMENT,
    RPC_PRODUCT_FAMILY,
    ReferenceInstrument,
    adapter_identity,
    optional_adapter_identity,
)
from perp_md.models import (
    ContractDirection,
    HistoryIssue,
    HistoryRange,
    Instrument,
    NativeUnit,
    ObservationTimeKind,
    OpenInterestCapabilities,
    OpenInterestObservation,
    OpenInterestResult,
    ValuationMethod,
)
from perp_md.normalization import contract_value_usd, number, proven_base_quantity
from perp_md.transport import JsonTransport

BINANCE_HISTORY_LIMIT = 500
BYBIT_HISTORY_LIMIT = 200
BYBIT_MARK_HISTORY_LIMIT = 1_000
GATE_HISTORY_LIMIT = 1_000
HISTORY_MAX_PAGES = 200
BINANCE_HISTORY_DAYS = 30
HISTORY_BUCKET_MS = 300_000
BITFINEX_HISTORY_LIMIT = 5_000
DEEPCOIN_HISTORY_LIMIT = 300
KUCOIN_HISTORY_LIMIT = 200
HTX_HISTORY_LIMIT = 200
HYPERLIQUID_SCOPED_PRODUCT_FAMILY = "HIP-3"
KRAKEN_HISTORY_DAYS = 6
KRAKEN_HISTORY_INTERVAL_SECONDS = 300
KRAKEN_TICKERS_URL = "https://futures.kraken.com/derivatives/api/v3/tickers"
KRAKEN_CHARTS_URL = "https://futures.kraken.com/api/charts/v1"
BINGX_API_URL = "https://open-api.bingx.com/openApi"


@dataclass
class NativeAdapter:
    transport: JsonTransport
    clock: Callable[[], float] = time.time

    async def close(self) -> None:
        return None

    @staticmethod
    def _issue(exc: Exception) -> HistoryIssue:
        detail = str(exc).strip()
        message = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        return HistoryIssue("history_unavailable", message)


def _integer_ms(value: Any, metric: str) -> int:
    parsed = number(value)
    if parsed < 0 or not parsed.is_integer():
        raise InvalidResponse(f"venue returned an invalid {metric} source timestamp")
    return int(parsed)


def _bingx_row(payload: Any, symbol: str, description: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        raise InvalidResponse(f"venue returned an invalid {description}")
    data = payload.get("data")
    rows = data if isinstance(data, list) else [data]
    matches = [
        row for row in rows if isinstance(row, dict) and row.get("symbol") == symbol
    ]
    if len(matches) != 1:
        raise DataUnavailable(
            f"instrument does not resolve to exactly one {description} row"
        )
    return matches[0]


def _require_identity(row: dict[str, Any], field: str, expected: str) -> None:
    if row.get(field) != expected:
        raise InvalidResponse("venue returned a mismatched instrument identity")


def _btse_rows(payload: Any, description: str) -> tuple[list[dict[str, Any]], int]:
    if (
        not isinstance(payload, dict)
        or payload.get("success") is not True
        or not isinstance(payload.get("data"), (dict, list))
    ):
        raise InvalidResponse(f"venue returned an invalid {description}")
    data = payload["data"]
    rows = data if isinstance(data, list) else [data]
    if any(not isinstance(row, dict) for row in rows):
        raise InvalidResponse(f"venue returned an invalid {description}")
    return rows, _integer_ms(payload.get("time"), description)


def _base_observation(
    timestamp: int,
    raw_value: Any,
    mark_value: Any,
    *,
    timestamp_kind: ObservationTimeKind = ObservationTimeKind.SOURCE,
) -> OpenInterestObservation:
    native = number(raw_value)
    mark = number(mark_value)
    return OpenInterestObservation(
        timestamp,
        native * mark,
        native,
        NativeUnit.BASE,
        mark,
        ValuationMethod.MARK_PRICE,
        OpenInterestValueV1(
            Decimal(str(native)), OpenInterestMeasure.BASE_QUANTITY
        ),
        timestamp_kind,
    )


def _inverse_contract_observation(
    instrument: Instrument, timestamp: int, raw_value: Any
) -> OpenInterestObservation:
    contracts = number(raw_value)
    return OpenInterestObservation(
        timestamp,
        contract_value_usd(instrument, contracts, None),
        contracts,
        NativeUnit.CONTRACTS,
        valuation=ValuationMethod.CONTRACT_VALUE,
    )




def _coded_rows(
    payload: Any, description: str, *, row_type: type = dict
) -> list[Any]:
    if (
        not isinstance(payload, dict)
        or str(payload.get("code")) != "0"
        or not isinstance(payload.get("data"), list)
        or any(not isinstance(row, row_type) for row in payload["data"])
    ):
        raise InvalidResponse(f"venue returned an invalid {description}")
    return payload["data"]


def _kucoin_data(payload: Any, description: str, expected: type) -> Any:
    if (
        not isinstance(payload, dict)
        or str(payload.get("code")) != "200000"
        or not isinstance(payload.get("data"), expected)
    ):
        raise InvalidResponse(f"venue returned an invalid {description}")
    return payload["data"]


def _contract_observation(
    instrument: Instrument, timestamp: int, raw_value: Any, mark_value: Any
) -> OpenInterestObservation:
    raw, mark = number(raw_value), number(mark_value)
    if raw < 0 or mark <= 0:
        raise InvalidResponse("venue returned invalid open interest or mark price")
    return OpenInterestObservation(
        timestamp,
        contract_value_usd(instrument, raw, mark),
        raw,
        NativeUnit.CONTRACTS,
        mark,
        ValuationMethod.MARK_PRICE
        if instrument.contract_direction is ContractDirection.LINEAR
        else ValuationMethod.CONTRACT_VALUE,
        proven_base_quantity(instrument, raw, NativeUnit.CONTRACTS),
    )


def _join_contract_history(
    instrument: Instrument,
    rows: list[dict[str, Any]],
    marks: dict[int, float],
) -> tuple[tuple[OpenInterestObservation, ...], int]:
    points: list[OpenInterestObservation] = []
    missing = 0
    for row in rows:
        timestamp = _integer_ms(row.get("ts"), "open-interest")
        raw = row.get("oi", row.get("openInterest"))
        if instrument.contract_direction is ContractDirection.INVERSE:
            contracts = number(raw)
            points.append(
                OpenInterestObservation(
                    timestamp,
                    contract_value_usd(instrument, contracts, None),
                    contracts,
                    NativeUnit.CONTRACTS,
                    valuation=ValuationMethod.CONTRACT_VALUE,
                )
            )
            continue
        mark = marks.get(timestamp)
        if mark is None:
            missing += 1
            continue
        points.append(_contract_observation(instrument, timestamp, raw, mark))
    return tuple(points), missing


def _partial_mark_issue(missing: int, total: int) -> HistoryIssue | None:
    if not missing:
        return None
    return HistoryIssue(
        "history_partial",
        f"mark-price history omitted {missing} of {total} open-interest buckets",
    )


async def _backward_dict_history(
    transport: JsonTransport,
    url: str,
    base: dict[str, Any],
    timestamp_field: str,
    limit: int,
    requested: HistoryRange | None,
    *,
    coded: bool = False,
    kucoin: bool = False,
    stop_on_short_page: bool = True,
) -> list[dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    page_end = base.get("endTime", base.get("endAt"))
    for _ in range(HISTORY_MAX_PAGES):
        request = dict(base)
        if page_end is not None:
            request["endAt" if kucoin else "endTime"] = page_end
        payload = await transport.get(url, request)
        page = (
            _coded_rows(payload, "open-interest history")
            if coded
            else _kucoin_data(payload, "open-interest history", list)
        )
        if any(not isinstance(row, dict) for row in page):
            raise InvalidResponse("venue returned an invalid open-interest history row")
        if not page:
            return [rows[key] for key in sorted(rows)]
        timestamps = [_integer_ms(row.get(timestamp_field), "open-interest") for row in page]
        start = requested.start_ms if requested else None
        end = requested.end_ms if requested else None
        for timestamp, row in zip(timestamps, page):
            if (start is None or timestamp >= start) and (end is None or timestamp <= end):
                rows[timestamp] = row
        oldest = min(timestamps)
        if (stop_on_short_page and len(page) < limit) or (
            start is not None and oldest <= start
        ):
            return [rows[key] for key in sorted(rows)]
        advanced = oldest - 1
        if page_end is not None and advanced >= page_end:
            raise PaginationError("open-interest history pagination did not advance")
        page_end = advanced
    raise PaginationError("open-interest history exceeded the bounded page limit")


def _htx_rows(payload: Any, description: str) -> tuple[list[dict[str, Any]], int]:
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or not isinstance(payload.get("data"), list)
        or any(not isinstance(row, dict) for row in payload["data"])
    ):
        raise InvalidResponse(f"venue returned an invalid {description}")
    return payload["data"], _integer_ms(payload.get("ts"), description)


def _htx_observation(
    instrument: Instrument, row: dict[str, Any], timestamp: int
) -> OpenInterestObservation:
    contracts = number(row.get("volume"))
    if contracts < 0:
        raise InvalidResponse("venue returned negative open interest")
    amount = row.get("amount")
    value = row.get("value")
    if instrument.contract_direction is ContractDirection.LINEAR:
        if value is None:
            raise DataUnavailable("venue omitted normalized linear open interest")
        notional = number(value)
        base_amount = (
            number(amount)
            if amount is not None
            else contracts * number(instrument.contract_multiplier)
        )
        base_quantity = OpenInterestValueV1(
            Decimal(str(base_amount)), OpenInterestMeasure.BASE_QUANTITY
        )
        valuation = ValuationMethod.VENUE_REPORTED
    elif instrument.contract_direction is ContractDirection.INVERSE:
        notional = contract_value_usd(instrument, contracts, None)
        base_quantity = None
        valuation = ValuationMethod.CONTRACT_VALUE
    else:
        raise InvalidInstrument("contract_direction is required for provider product routing")
    return OpenInterestObservation(
        timestamp,
        notional,
        contracts,
        NativeUnit.CONTRACTS,
        valuation=valuation,
        base_quantity=base_quantity,
    )


def _htx_history_rows(
    payload: Any, requested: HistoryRange | None
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise InvalidResponse("venue rejected the open-interest history request")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("tick"), list):
        raise DataUnavailable("venue returned no open-interest history")
    rows: dict[int, dict[str, Any]] = {}
    for row in data["tick"]:
        if not isinstance(row, dict):
            raise InvalidResponse("venue returned an invalid open-interest history row")
        timestamp = _integer_ms(row.get("ts"), "open-interest")
        if requested is None or (
            (requested.start_ms is None or timestamp >= requested.start_ms)
            and (requested.end_ms is None or timestamp <= requested.end_ms)
        ):
            rows[timestamp] = row
    return [rows[key] for key in sorted(rows)]
