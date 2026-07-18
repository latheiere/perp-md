from importlib.metadata import PackageNotFoundError, version

from perp_md.client import OpenInterestClient
from perp_md.errors import (
    AdapterUnavailable,
    DataUnavailable,
    InvalidInstrument,
    InvalidResponse,
    PaginationError,
    PerpMdError,
    RequestError,
)
from perp_md.history import find_resume_time
from perp_md.models import (
    ContractDirection,
    HistoryIssue,
    HistoryRange,
    Instrument,
    NativeUnit,
    OpenInterestCapabilities,
    OpenInterestObservation,
    OpenInterestResult,
    ValuationMethod,
)

try:
    __version__ = version("perp-md")
except PackageNotFoundError:
    __version__ = "0.0.0+uninstalled"

__all__ = [
    "AdapterUnavailable",
    "ContractDirection",
    "DataUnavailable",
    "HistoryIssue",
    "HistoryRange",
    "Instrument",
    "InvalidInstrument",
    "InvalidResponse",
    "NativeUnit",
    "OpenInterestCapabilities",
    "OpenInterestClient",
    "OpenInterestObservation",
    "OpenInterestResult",
    "PaginationError",
    "PerpMdError",
    "RequestError",
    "ValuationMethod",
    "find_resume_time",
]
