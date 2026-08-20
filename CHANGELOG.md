# Changelog

Notable changes to `perp-md` are recorded here. Releases follow
[Semantic Versioning](https://semver.org/).

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
