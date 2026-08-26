# Changelog

Notable changes to `perp-md` are recorded here. Releases follow
[Semantic Versioning](https://semver.org/).

## 0.2.6 - 2026-08-26

- Preserve funding duration evidence when source settlement boundaries carry
  bounded millisecond jitter around nominal whole-second instants, while
  retaining exact source timestamps and rejecting wider or ambiguous windows.

## 0.2.5 - 2026-08-24

- Preserve exact settlement spacing as observed funding-window evidence when
  historical rows omit a separately reported duration.
- Resolve a current funding cycle from provider settlement boundaries when a
  current snapshot omits direct interval metadata.

## 0.2.4 - 2026-08-24

- Report a documented hourly funding frequency as an explicit duration when a
  continuous-accrual perpetual protocol standardizes rates and realizes
  balances on that frequency.

## 0.2.3 - 2026-08-24

- Preserve current funding intervals when native endpoints or optional-provider
  raw payloads expose unambiguous duration evidence.
- Acquire indicative funding snapshots separately from settled history when a
  provider exposes distinct current and historical endpoints.
- Use the provider's recommended global REST API domain for public requests.

## 0.2.2 - 2026-08-20

- Observe cached transport task completion after shielded callers are cancelled,
  preventing later asynchronous failures from escaping event-loop supervision.
- Close partially initialized optional-provider adapters when market discovery
  is interrupted.

## 0.2.1 - 2026-08-10

- Reworked the package landing page into a compact installation, usage,
  boundary, documentation, and support guide. Public APIs and runtime behavior
  are unchanged.

## 0.2.0 - 2026-08-10

- Added CDM-native instrument references, structured capability assessment,
  operation planning, typed funding acquisition, lossless funding wire
  encoding, and deterministic declared-coverage manifests.
