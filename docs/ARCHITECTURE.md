# Architecture

## Modules

- `models` defines immutable instrument, history range, observation, result,
  capability, and history-issue values.
- `errors` defines the public exception hierarchy.
- `normalization` validates numeric fields and converts contract-count OI to
  normalized USD notional.
- `transport` implements bounded asynchronous JSON requests, per-host
  concurrency, connection pooling, and short-lived request deduplication.
- `adapters.native` contains native endpoint selection, payload parsing,
  current OI normalization, and historical pagination.
- `adapters.ccxt` contains the optional CCXT exchange lifecycle, symbol
  resolution, unified OI parsing, and venue-specific CCXT extensions.
- `client` selects an adapter and owns transport and adapter shutdown.
- `history` calculates a resume timestamp from persisted observation times.

## Request flow

1. `OpenInterestClient.fetch` receives an `Instrument`, optional
   `HistoryRange`, and history flag.
2. The client selects a registered native adapter by normalized venue name. If
   none is registered, it selects the configured fallback adapter.
3. The adapter issues public requests through `JsonTransport` or its optional
   provider runtime.
4. The adapter validates the payload and constructs an
   `OpenInterestObservation` for the current value.
5. When history is enabled, the adapter fetches bounded pages, filters the
   requested range, deduplicates timestamps, and sorts observations.
6. A history failure after current success becomes `HistoryIssue`; a current
   failure raises a `PerpMdError` subtype.

## Resource lifecycle

`HttpxTransport` creates its `httpx.AsyncClient` on the first request. It owns
global and per-host semaphores and a cache of in-flight or recent identical
requests. `close` cancels unfinished cached requests and closes the HTTP client.

`CcxtAdapter` imports CCXT on first fallback use, creates one exchange instance
per venue, loads each venue catalog once, and closes all exchange instances on
shutdown.

`OpenInterestClient` supports explicit `close` and asynchronous context-manager
cleanup. An injected transport remains owned by its caller.

## Adapter contract

Each adapter implements:

- `supports(Instrument) -> bool`
- `capabilities(Instrument) -> OpenInterestCapabilities`
- `fetch(Instrument, HistoryRange | None, include_history=...)`
- `close()`

Native adapter tests use recorded JSON fixtures and injected transports. The
offline suite covers successful normalization, zero values, pagination,
deduplication, malformed history, partial results, and exact CCXT resolution.

## Package boundaries

The package contains no scheduler, database, CSV format, retention policy,
market catalog, cross-market aggregation, completeness definition, web API,
retry queue, or visualization behavior.
