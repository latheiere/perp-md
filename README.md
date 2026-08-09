# perp-md

`perp-md` is a typed asynchronous Python library for current and historical
perpetual-market open interest and funding. Separate clients present stable
contracts while keeping provider protocols, pagination, validation,
normalization evidence, and optional CCXT integration behind adapter
boundaries.

Provider-independent economics and native-identity envelopes use the public `cdm` namespace from
`crypto-derivative-markets`. `perp-md` owns acquisition protocols, provider
identity requirements, source evidence, retrieval limits, and declared adapter coverage; it
does not define a parallel instrument or datapoint vocabulary.

The package contains no persistence, scheduling, catalog discovery,
aggregation, chart policy, or application APIs. Market identity, storage, and
observation use remain outside the library.

## Status

The public API is alpha and follows Semantic Versioning.

## Behavior

- `fetch_reference(provider_id, reference, ...)` accepts a CDM
  `InstrumentReferenceV1` unchanged; the legacy `Instrument` API remains a
  compatibility path.
- Provider-native identities are selected by exact CDM role and namespace.
  Missing and ambiguous selections are structured gaps; values are never
  guessed, rewritten, or selected by input order.
- Missing and unsupported values are never represented as zero.
- Current observations remain usable when optional history fails.
- Native quantities, units, marks, timestamps, and valuation methods are
  preserved alongside normalized USD notional.
- Proven canonical base OI quantity and reporting notional are exposed through
  CDM `OpenInterestValueV1` values while the legacy scalar fields remain
  available.
- Funding observations distinguish indicative and settled rates, timestamp
  meaning, calculation lineage, and interval evidence.
- The versioned funding observation wire codec preserves the exact CDM sample
  and acquisition evidence for storage or distribution without adding identity.
- Aggregate derivative protocols use response-level source time when
  individual market timestamps describe unrelated last-trade activity.
- History ranges are bounded, deduplicated, ordered, and protected by finite
  pagination limits.
- Base-unit linear history is normalized only against exact-timestamp mark
  candles; missing joins are reported as structured partial history.
- History capabilities and requests use the cadence actually supported by each
  venue protocol.
- Runtime assessment accepts an incomplete CDM reference and reports
  structured field-path and identity-selector issues without embedding
  consumer or catalog concepts.
- Optional adapter support is probed from exact installed provider methods;
  consumers do not copy runtime feature identifiers or infer support from
  package presence.
- Runtime operation plans expose only generic scheduler constraints, including
  proven fixed cadence, bounded lookback, and explicit-start requirements.
- Versioned adapter manifests drive machine-readable coverage JSON and human
  documentation inputs from one source.
- Static native product families use exact catalog evidence names and contexts;
  open-ended runtime-scoped templates are declared but never treated as exact
  catalog matches.
- Native adapters are preferred when registered; fallback is explicit.
- External I/O is asynchronous, bounded, injectable, and independently
  testable.
- The package contains no consumer-specific storage or presentation behavior.

The complete contract is in [docs/CONTRACT.md](docs/CONTRACT.md), and package
boundaries are described in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

CCXT support is optional and selected through the `ccxt` extra.

`perp-md-coverage` writes the deterministic declared adapter coverage manifest
as JSON. The bundled producer schema has the stable identifier
`urn:perp-md:schema:declared-coverage:1`.

## License

Apache-2.0.
