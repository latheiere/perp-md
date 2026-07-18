# perp-md

`perp-md` is a typed asynchronous Python library for current and historical
perpetual-market open interest. It presents one stable contract while keeping
venue protocols, pagination, validation, and optional CCXT integration behind
adapter boundaries.

The package contains no persistence, scheduling, catalog discovery,
aggregation, chart policy, or application APIs. Market identity, storage, and
observation use remain outside the library.

## Status

The public API is alpha, follows Semantic Versioning, and covers open interest.

## Behavior

- Venue-native symbols are accepted without symbol guessing or rewriting.
- Missing and unsupported values are never represented as zero.
- Current observations remain usable when optional history fails.
- Native quantities, units, marks, timestamps, and valuation methods are
  preserved alongside normalized USD notional.
- History ranges are bounded, deduplicated, ordered, and protected by finite
  pagination limits.
- Native adapters are preferred when registered; fallback is explicit.
- External I/O is asynchronous, bounded, injectable, and independently
  testable.
- The package contains no consumer-specific storage or presentation behavior.

The complete contract is in [docs/CONTRACT.md](docs/CONTRACT.md), and package
boundaries are described in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

CCXT support is optional and selected through the `ccxt` extra.

## License

Apache-2.0.
