# Coding agent guide

This repository is a generic public market-data library. It must remain
independent of every consumer, catalog, persistence system, scheduler, web
application, and deployment environment.

## Boundaries

- Preserve venue-native identity and source timestamps.
- Never infer missing observations as zero.
- Keep the public contract typed and transport-independent.
- Keep venue payload knowledge inside adapter modules.
- Require explicit contract metadata for conversions that cannot be proven
  from a venue-reported normalized value.
- Bound every external request and pagination loop.
- Keep optional providers lazy and optional.
- Do not add concrete instrument identities, credentials, private endpoints,
  consumer schemas, storage formats, or product-specific policy.
- Offline tests must not access the network.

## Workflow

1. Run `git status --short --branch` before changes.
2. Update `docs/CONTRACT.md` for public behavior changes.
3. Add success, malformed, and partial-history fixtures for adapter changes.
4. Run `python -m pytest -q`, `python -m build`, and
   `python -m twine check dist/*` before handoff.
5. Verify the built wheel installs and reports the declared version.
6. Do not publish a release or package-index artifact unless explicitly asked.
