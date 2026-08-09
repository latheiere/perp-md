# Architecture

## Modules

- `models` defines legacy acquisition inputs, result envelopes, provider source
  evidence, and history issues. Neutral instrument, OI, funding, datapoint, and
  lineage contracts come from `cdm`.
- `capabilities` combines CDM references with adapter declarations for
  structured runtime assessment and deterministic manifest export.
- `identity` resolves exact CDM role-and-namespace selectors and supplies an
  internal adapter view without changing the caller's reference.
- `funding_wire` serializes the acquisition evidence envelope while delegating
  funding economics and lineage serialization to CDM.
- `errors` defines the public exception hierarchy.
- `normalization` validates numeric fields and converts contract-count OI to
  normalized USD notional.
- `transport` implements bounded asynchronous JSON requests, per-host
  concurrency, connection pooling, and short-lived request deduplication.
- `adapters.native` contains native endpoint selection, payload parsing,
  current OI normalization, and historical pagination.
- `adapters.funding` contains native funding acquisition, relative-rate
  normalization, temporal semantics, evidence capture, and bounded history.
- `adapters.ccxt` contains the optional CCXT exchange lifecycle, symbol
  resolution, unified OI parsing, and venue-specific CCXT extensions.
- `adapters.ccxt_funding` adds optional-runtime funding without coupling the
  two public clients.
- `adapters.manifests` is the code-adjacent declarative source for assessment
  and coverage export.
- `client` and `funding_client` independently select adapters and own their
  transport and adapter shutdown.
- `history` calculates a resume timestamp from persisted observation times.

## Request flow

1. `OpenInterestClient.fetch_reference` receives a provider identifier, an
   unchanged CDM `InstrumentReferenceV1`, optional `HistoryRange`, and history
   flag. `fetch(Instrument, ...)` remains the compatibility flow.
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

Funding follows the same primary-current and optional-history control flow,
while preserving indicative versus settled observations and the evidence for
any normalization. The two clients do not call or own one another.

## Capability flow

1. An adapter manifest maps provider-native product-family terminology to
   a CDM `InstrumentScenarioV1`. Static terminology is exact catalog evidence
   identified by normalized name and evidence context; runtime-scoped family
   templates remain explicitly non-joinable.
2. A caller supplies an incomplete `cdm.InstrumentReferenceV1`, a CDM
   datapoint kind, and optional temporal or market-observation evidence.
3. `assess_capability` selects only scenarios compatible with known descriptor
   values and reports missing economic, identity, observation, or runtime
   requirements as structured field paths.
4. A client's `assess_runtime` probes exact installed adapter methods and adds
   that evidence without requiring a consumer to know runtime feature IDs.
5. `plan_reference` combines the selected declaration with adapter-owned fixed
   interval, bounded-lookback, and explicit-start facts. It exposes only
   generic scheduler inputs and leaves unknown constraints unset.
6. The JSON exporter embeds exact CDM scenario, datapoint, lineage, and native
   identity selector wire
   contracts in the producer-owned acquisition coverage schema.

The manifest is declarative coverage evidence. Executable payload parsing and
normalization remain in adapters, and consumers never interpret manifest rows
as replacement adapter logic. Economic classification remains in CDM;
provider-native names and endpoint constraints remain in `perp-md`.

## Resource lifecycle

`HttpxTransport` creates its `httpx.AsyncClient` on the first request. It owns
global and per-host semaphores and a cache of in-flight or recent identical
requests. `close` cancels unfinished cached requests and closes the HTTP client.
Symbol-independent aggregate snapshots use one identical transport key, so
concurrent instrument reads share the bounded request and parse their exact
venue-native rows independently.

`CcxtAdapter` can instantiate an unloaded exchange to inspect its exact
provider method support without network catalog loading. It imports CCXT on
first fallback use, creates one exchange instance
per venue, loads each venue catalog once, and closes all exchange instances on
shutdown.

`OpenInterestClient` supports explicit `close` and asynchronous context-manager
cleanup. An injected transport remains owned by its caller.

`FundingClient` follows the same ownership contract. Applications that create
both clients close both independently; a future runtime may place either client
in a separate process without changing the library boundary.

## Adapter contract

Compatibility adapters implement:

- `supports(Instrument) -> bool`
- `capabilities(Instrument) -> OpenInterestCapabilities`
- `fetch(Instrument, HistoryRange | None, include_history=...)`
- `close()`

Funding adapters implement the corresponding typed funding capability and
result methods. Reference-based client methods resolve CDM selectors into the
same internal adapter inputs. Both adapter families preserve exact provider
identity and do not query an external catalog.

Native adapter tests use recorded JSON fixtures and injected transports. The
offline suite covers successful normalization, zero values, pagination,
deduplication, native history cadence, malformed history, partial results,
scoped perpetual universes, exact optional-provider resolution, funding source
evidence, honest interval semantics, and structured capability assessment.

## Package boundaries

The package contains no scheduler, database, CSV format, retention policy,
market catalog, cross-market aggregation, completeness definition, web API,
retry queue, or visualization behavior.
