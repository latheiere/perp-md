# Public contract

## Instrument identity

The primary service-facing input is `cdm.InstrumentReferenceV1`. It keeps the
pure economic `InstrumentDescriptorV1` beside zero or more opaque
`NativeIdentityV1` values. `OpenInterestClient.fetch_reference` and
`FundingClient.fetch_reference` accept a `provider_id` and that reference
unchanged. The legacy `Instrument` input and `fetch` methods remain a
compatibility path and are not the cross-module identity contract.

Every provider operation declares exact CDM `NativeIdentitySelectorV1`
requirements by semantic role and protocol namespace. Selection uses exact
role-and-namespace equality. A unique match is passed unchanged to the
adapter. A missing or ambiguous match produces a structured capability issue
or `NativeIdentityResolutionError`; aliases are never resolved by order.
Adapters do not add prefixes, strip suffixes, split identifiers, select a
default settlement asset, or otherwise synthesize endpoint identity.

When an aggregate protocol needs a separate route and instrument identity,
each value has its own selector. The route is supplied as a product-family
identity and the catalog instrument is supplied as an instrument identity.
Settlement-routed endpoints similarly require a settlement-asset identity.
These are provider acquisition requirements owned by `perp-md`, while the
identity value types and selector semantics are owned by CDM.

Contract-count OI normalization requires explicit contract direction and
multiplier. Missing required metadata produces `InvalidInstrument`, never a
guessed conversion. For a linear contract, `contract_multiplier` is canonical
base units per contract and normalized value is contract count multiplied by
the multiplier and contemporaneous positive mark. For an inverse contract,
`contract_multiplier` is USD quote notional per contract and normalized value
is contract count multiplied by the multiplier.

An adapter reading a venue-wide aggregate response selects a row only by exact
equality with the resolved operation identity. No matching row or multiple matching rows
produce `DataUnavailable`; an invalid envelope, malformed row identity, or
invalid required observation field produces `InvalidResponse`.

For aggregate derivative tickers that publish base-unit OI for linear
contracts and contract-count OI for inverse contracts, callers provide an
explicit contract direction. Linear observations are normalized with the
source mark price. Inverse observations additionally require the published
quote-notional contract multiplier. A valid positive inverse mark is retained
when available but does not gate contract-value normalization. Current
observations use the aggregate response timestamp rather than the last-trade
time of an individual market.

## Observations

`OpenInterestObservation` contains:

- the source observation time in Unix milliseconds;
- normalized non-negative USD notional;
- the venue-native quantity and unit when published;
- the mark used for conversion when applicable;
- a valuation method describing how normalization was obtained.
- an optional proven canonical base quantity as CDM
  `OpenInterestValueV1`.

The `notional` property presents the backward-compatible `value_usd` result as
a CDM reporting-denominated `OpenInterestValueV1`. The scalar `value_usd` field
is retained for compatibility.

Canonical base quantity is absent unless the provider reports base quantity
directly or explicit linear-contract metadata proves the conversion. A
normalized notional alone does not prove a base quantity. Contract-count
conversion requires a positive base-denominated multiplier. Consumers must not
repeat adapter-specific unit assumptions when this typed field is absent.

Zero is a valid observation. Absence, unsupported capabilities, malformed
payloads, and transport failures are errors and are never converted to zero.
All numeric values must be finite.

## Current and history independence

`OpenInterestClient.fetch` and `FundingClient.fetch` always treat the current
observation as primary.
When history is requested and fails after current succeeds, the result contains
the current observation plus a structured `HistoryIssue`. Callers decide
whether partial results are acceptable.

History output is ordered by timestamp and deduplicated by timestamp. An
explicit `HistoryRange` is inclusive at both endpoints. Adapters clamp ranges
to documented venue retention and the latest complete native bucket. A paged
adapter continues through a short response when source timestamps show that
the requested range has not yet been traversed; sparse native buckets do not
silently truncate later available history.

When a five-minute analytics protocol publishes OI as four-value OHLC tuples,
the adapter validates the complete tuple and uses its close. Linear-contract
history is joined to mark-price candles only on exact source timestamps. A
missing corresponding mark omits that unprovable USD observation and produces
a `history_partial` issue while preserving every exactly matched observation.
The supported lookback for this protocol is conservatively bounded to six
whole days, which fits below the per-response record ceiling for both OI and
marks at five-minute cadence.

Every linear-contract history source that requires mark conversion joins the
native quantity to a contemporaneous mark on exact source timestamps. It never
uses the current mark for historical normalization. Missing joins produce a
structured partial-history result.

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

Capability assessment accepts `cdm.InstrumentReferenceV1`, or an incomplete
`cdm.InstrumentDescriptorV1` plus typed `native_identities`. The legacy
`Instrument` input can still be projected into the economic descriptor by
`instrument_descriptor_from_instrument`. There is no parallel provider
identity model in this package.

`assess_capability` and each metric client's `assess` method return a structured
state, alternatives, and issues with stable field paths. Scenario selection is
generic: known CDM dimensions are compared with manifest
`InstrumentScenarioV1` values, unknown dimensions become requirements, and
contradictory scenarios are excluded. Provider-specific identity, endpoint,
and runtime requirements come only from adapter manifests. Consumers do not
copy provider-specific requirements or economic classification tables.

Assessment targets CDM `DataPointKind` and optionally `TemporalMode` or an exact
`DataPointDefinitionV1`. Contract-count OI, base-quantity OI,
reporting-denominated OI notional, indicative funding, settled funding, and
funding interval are independent declarations. Runtime-conditional support
requires explicit runtime feature evidence. Support for one datapoint or
temporal mode does not imply support for another.

`OpenInterestClient.assess_runtime` and `FundingClient.assess_runtime` probe
the configured adapter and supply its exact proven runtime features to the
same assessment engine. Consumers do not inspect optional dependencies,
provider method tables, or manifest runtime feature identifiers.
`runtime_features(provider_id, reference)` is available for adapter and
deployment diagnostics, but acquisition consumers normally use the structured
assessment result. Installed-package presence alone never proves that a
provider runtime implements an optional operation.

Each metric client also exposes
`plan_reference(provider_id, reference, datapoint=..., temporal_mode=...)`.
It returns an `AcquisitionPlan` with the runtime `assessment` and an optional
`PlannedRetrieval`. `plan.status` and `plan.issues` are convenience views of the
assessment. A supported, unambiguous retrieval plan contains `request_scope`,
`history_scope`, `pagination`, `fixed_interval_seconds`,
`max_lookback_seconds`, and `requires_explicit_start`. Unknown interval or
lookback bounds remain `None`; protocol schedules are never converted into an
invented duration. Unsupported operations or supported alternatives that do
not agree on retrieval shape have `retrieval=None`.

The plan deliberately excludes adapter identifiers, route values, optional
runtime feature identifiers, and request-cost estimates. The library does not
publish a request-cost hint until the underlying protocols provide a stable,
comparable unit that a scheduler can rely on.

`coverage_manifest` returns the machine-readable
`acquisition.coverage/v1` manifest and `coverage_manifest_json` returns its
deterministic JSON representation. The `perp-md-coverage` command writes the
same JSON to standard output. `load_coverage_schema` reads the bundled JSON
Schema with `$id` `urn:perp-md:schema:declared-coverage:1`.
The acquisition schema references the pinned CDM scenario, datapoint, and
lineage schema identifiers instead of copying their economic definitions;
validators register those CDM schemas in the same schema registry.

Each mapping contains provider-native product-family names and one exact CDM
`InstrumentScenarioV1`. Each capability embeds exact CDM
`DataPointDefinitionV1` and `MeasurementLineageV1` wire values, declared state,
source observations, typed CDM identity selectors, the remaining requirement
categories, retrieval shape, and
limitations. The manifest describes the library's declared acquisition
ceiling, not catalog contents, deployment configuration, or observed health.
Its default `generated_at` is the source declaration revision timestamp, so
the release artifact is reproducible; callers generating a deployment snapshot
may provide an explicit timezone-aware timestamp.

Static product-family names are exact native catalog evidence, not editorial
labels. A catalog join requires equality of both normalized name and evidence
context, together with venue and CDM scenario equality. A generic normalized
catalog product does not substitute for a distinct provider product family,
and the same text in materially different contexts remains distinct evidence.
This prevents same-scenario families from becoming ambiguous.

When a protocol's concrete product-family name embeds an open-ended runtime
scope, the manifest declares a visibly non-exact template context. Such a row
documents adapter coverage but is intentionally ineligible for an exact static
catalog join. Acquisition still resolves the exact route and instrument from
the caller's CDM native identities; neither the library nor documentation
tooling derives a concrete family from the template.

## Funding

`FundingClient` is independent from `OpenInterestClient`; neither client owns
or coordinates the other's lifecycle. `FundingObservation.sample` is a CDM
`FundingSampleV1` whose rate is a finite decimal fraction. The `rate`, `kind`,
`interval`, and `timestamp_ms` properties are compatibility views.

CDM `observed_at` is set only when the source declares an observation time, and
`effective_at` is set for next or settled rates. Retrieval time is never placed
in either field. It is retained in the acquisition-only
`ProviderFundingEvidence`. The compatibility `timestamp_ms` property falls
back to retrieval time only when an indicative source supplies no observation
time. A history-only source may return the latest settled observation as
`FundingResult.current`; it is never relabeled as indicative.

`ProviderFundingEvidence` identifies the provider source observation, retrieval
time, raw source value, and supporting mark. Ordered economic calculation
lineage is represented by CDM `MeasurementLineageV1` and
`DerivationStepV1`. When an absolute amount is normalized, the contemporaneous
positive mark is retained in provider evidence and the direction-specific
formula has a stable method identifier in CDM lineage.

The stable persistence and distribution envelope has schema identifier
`urn:perp-md:schema:funding-observation:1`. It contains only `schema_id`, the
exact CDM `FundingSampleV1` wire value under `sample`, and acquisition evidence
under `provider_evidence`. Provider identity and instrument identity remain in
their owning envelopes and are not duplicated in this observation contract.

`funding_observation_to_data` and `funding_observation_from_data` provide the
strict typed object codec; `funding_observation_to_json` and
`funding_observation_from_json` provide deterministic JSON. Decoding rejects
unknown fields, non-canonical decimals or UTC timestamps, unsupported schema
identifiers, and invalid embedded CDM samples. `load_funding_observation_schema`
returns the bundled producer-owned JSON Schema, which references rather than
copies the CDM funding-sample schema.

Every sample carries CDM `FundingIntervalV1`. Its kind is
`explicit_duration`, `observed_window`, `protocol_schedule`, or `unspecified`.
An explicit duration is reported only when the source endpoint or governing
contract specification supplies an unambiguous rate frequency, or explicitly
identifies consecutive funding boundaries whose difference defines the
interval. Raw provider fields with named time units are valid duration evidence
even when an optional abstraction omits its normalized interval field. A
protocol schedule is not converted into invented bounds. Regular spacing
between history observations is not treated as interval evidence. A current
source-reported interval is not applied retroactively to historical
observations unless the source proves that association.

Funding history is ordered and deduplicated by settlement timestamp. A
malformed or unavailable optional history preserves a valid current result and
returns `HistoryIssue`. A full-retained history endpoint requires an explicit
start and enforces a finite row bound; omitting the start produces a structured,
non-retryable history issue without discarding current data.

## Errors

All expected library failures derive from `PerpMdError`:

- `AdapterUnavailable`: no configured adapter can serve the venue.
- `CapabilityUnavailable`: a structured preflight assessment prevents a
  reference-based acquisition.
- `DataUnavailable`: the requested market datapoint is unsupported or absent.
- `FundingObservationDecodeError`: a persisted acquisition envelope violates
  the versioned wire contract; the exception carries a code and field path.
- `InvalidInstrument`: caller-supplied identity or contract metadata is invalid.
- `NativeIdentityResolutionError`: an exact CDM selector is missing or
  ambiguous; the exception carries the CDM selection result.
- `InvalidResponse`: the venue returned an invalid or incomplete payload.
- `PaginationError`: a bounded history traversal could not safely progress.
- `RequestError`: bounded external I/O failed.

Error messages do not name or depend on any catalog, service, database, or UI.

## Compatibility

The public imports re-exported from `perp_md` form the supported API. Adapter
internals and venue payload parsers are not public. In `0.x` releases, minor
versions may change the public API and patch versions preserve compatibility.
From `1.0.0`, breaking public changes increment the major version.
