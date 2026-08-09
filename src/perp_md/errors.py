from __future__ import annotations

from typing import Any

from cdm import NativeIdentitySelectionV1


class PerpMdError(RuntimeError):
    """Base class for expected perp-md failures."""


class AdapterUnavailable(PerpMdError):
    """No configured adapter can serve an instrument's venue."""


class DataUnavailable(PerpMdError):
    """The requested market datapoint is unsupported or absent."""


class InvalidInstrument(PerpMdError):
    """Required caller-supplied instrument metadata is invalid or missing."""


class NativeIdentityResolutionError(InvalidInstrument):
    """An exact native identity selector is missing or ambiguous."""

    def __init__(self, selection: NativeIdentitySelectionV1) -> None:
        self.selection = selection
        selector = selection.selector
        super().__init__(
            "native identity resolution is "
            f"{selection.status.value} for role={selector.role.value}, "
            f"namespace={selector.namespace}"
        )


class CapabilityUnavailable(PerpMdError):
    """A structured capability assessment prevents acquisition."""

    def __init__(self, assessment: Any) -> None:
        self.assessment = assessment
        super().__init__(f"acquisition capability is {assessment.status.value}")


class FundingObservationDecodeError(PerpMdError, ValueError):
    """A funding acquisition envelope does not satisfy its wire contract."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code} at {path}: {message}")


class InvalidResponse(PerpMdError):
    """A venue returned an invalid or incomplete payload."""


class PaginationError(PerpMdError):
    """A bounded history traversal could not safely progress."""


class RequestError(PerpMdError):
    """A bounded external request failed."""
