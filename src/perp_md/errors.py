from __future__ import annotations


class PerpMdError(RuntimeError):
    """Base class for expected perp-md failures."""


class AdapterUnavailable(PerpMdError):
    """No configured adapter can serve an instrument's venue."""


class DataUnavailable(PerpMdError):
    """Open interest is unsupported or absent for the instrument."""


class InvalidInstrument(PerpMdError):
    """Required caller-supplied instrument metadata is invalid or missing."""


class InvalidResponse(PerpMdError):
    """A venue returned an invalid or incomplete payload."""


class PaginationError(PerpMdError):
    """A bounded history traversal could not safely progress."""


class RequestError(PerpMdError):
    """A bounded external request failed."""
