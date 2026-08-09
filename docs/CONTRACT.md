# Public contract

## Instrument identity

`Instrument` is supplied by the caller. `venue` and `symbol` are opaque,
venue-native identifiers. The library never derives a market by removing
multipliers, separators, settlement suffixes, or other symbol components.

For venue protocols with named perpetual universes, a namespace embedded in
`symbol` selects that native scope. A caller may instead provide a recognized
venue-native product descriptor containing the scope when `symbol` is local to
the scoped universe. The supported scoped-product descriptor is
`HIP-3:<native-scope>`. Ordinary product labels without a descriptor separator
do not select a scope, and bare product scopes are not supported. When a symbol
namespace and scoped product descriptor are both present they must agree.
Unsupported descriptor families, missing or malformed scopes, and conflicting
metadata produce `InvalidInstrument`. An instrument without a symbol namespace
or scoped product descriptor continues to use the venue's default universe.

Contract-count OI normalization requires explicit contract direction and
multiplier. Missing required metadata produces `InvalidInstrument`, never a
guessed conversion. For a linear contract, `contract_multiplier` is canonical
base units per contract and normalized value is contract count multiplied by
the multiplier and contemporaneous positive mark. For an inverse contract,
`contract_multiplier` is USD quote notional per contract and normalized value
is contract count multiplied by the multiplier.

An adapter reading a venue-wide aggregate response selects a row only by exact
equality with `Instrument.symbol`. No matching row or multiple matching rows
produce `DataUnavailable`; an invalid envelope, malformed row identity, or
invalid required observation field produces `InvalidResponse`.

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
to documented venue retention and the latest complete native bucket. A paged
adapter continues through a short response when source timestamps show that
the requested range has not yet been traversed; sparse native buckets do not
silently truncate later available history.

Optional-provider adapters request a venue-supported native history cadence,
and capability intervals describe that requested cadence. A documented
record-count bound may be reported as a conservative whole-day lookback. If a
provider runtime omits advertised history support or returns malformed history
after current OI succeeds, the result preserves current OI and reports a
structured `HistoryIssue`.

## Capabilities

Capabilities report current and history availability, native history
granularity, maximum lookback when known, and required instrument metadata.
They describe adapter behavior, not caller configuration, storage coverage, or
rate-limit feasibility. A current-only aggregate contract-ticker adapter
reports `current=True`, `history=False`, and the generic contract fields needed
to normalize venue-reported contract counts.

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
