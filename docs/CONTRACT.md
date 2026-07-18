# Public contract

## Instrument identity

`Instrument` is supplied by the caller. `venue` and `symbol` are opaque,
venue-native identifiers. The library never derives a market by removing
multipliers, separators, settlement suffixes, or other symbol components.

Contract-count OI normalization requires explicit contract direction and
multiplier. Missing required metadata produces `InvalidInstrument`, never a
guessed conversion.

## Observations

`OpenInterestObservation` contains:

- the source observation time in Unix milliseconds;
- normalized non-negative USD notional;
- the venue-native quantity and unit when published;
- the mark used for conversion when applicable;
- a valuation method describing how normalization was obtained.

Zero is a valid observation. Absence, unsupported capabilities, malformed
payloads, and transport failures are errors and are never converted to zero.
All numeric values must be finite.

## Current and history independence

`OpenInterestClient.fetch` always treats the current observation as primary.
When history is requested and fails after current succeeds, the result contains
the current observation plus a structured `HistoryIssue`. Callers decide
whether partial results are acceptable.

History output is ordered by timestamp and deduplicated by timestamp. An
explicit `HistoryRange` is inclusive at both endpoints. Adapters clamp ranges
to documented venue retention and the latest complete native bucket.

## Capabilities

Capabilities report current and history availability, native history
granularity, maximum lookback when known, and required instrument metadata.
They describe adapter behavior, not caller configuration, storage coverage, or
rate-limit feasibility.

## Errors

All expected library failures derive from `PerpMdError`:

- `AdapterUnavailable`: no configured adapter can serve the venue.
- `DataUnavailable`: the metric is unsupported or absent.
- `InvalidInstrument`: caller-supplied identity or contract metadata is invalid.
- `InvalidResponse`: the venue returned an invalid or incomplete payload.
- `PaginationError`: a bounded history traversal could not safely progress.
- `RequestError`: bounded external I/O failed.

Error messages do not name or depend on any catalog, service, database, or UI.

## Compatibility

The public imports re-exported from `perp_md` form the supported API. Adapter
internals and venue payload parsers are not public. In `0.x` releases, minor
versions may change the public API and patch versions preserve compatibility.
From `1.0.0`, breaking public changes increment the major version.
