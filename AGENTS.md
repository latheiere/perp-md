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

## General, reusable development descriptions

In all agent-authored development prose and metadata, describe every defect,
feature, requirement, behavior, fix, and test scenario as reusable system
behavior, never as only one observed instance or one named subject. This applies
to pull request titles, descriptions, comments, and reviews; commit messages;
issue text; documentation; changelogs; release notes; status reports; and
similar artifacts.

Every description must be concrete about the affected system area, the
behaviorally relevant scope or category, the triggering conditions, and the
observed or intended outcome. It must also be generic enough to cover all known
equivalent cases. Retain distinctions that affect behavior; replace incidental
instance details such as names, domain identifiers, sample values, exact
timestamps, and one-off circumstances with the narrowest truthful reusable
category.

Do not overgeneralize beyond the evidence. When behavior is limited to a
particular asset class, market, instrument type, platform, protocol, data type,
workflow state, or other meaningful category, state that category. Do not name
or define the behavior around only the specific member on which it was
observed. In particular, do not expose concrete asset names, tickers, symbols,
trading pairs, contract codes, or venue-specific listings in agent-authored
development prose.

Write rules, definitions, acceptance criteria, and guidance from the invariant
or behavioral boundary, not from an illustrative case. An example must never
introduce a requirement, exception, scope restriction, or definition that is
absent from the general statement. When concrete examples are useful, place
them in a clearly labeled non-normative section, provide at least two materially
different examples, and state that they illustrate rather than define or limit
the rule.

Before publishing or committing an artifact, inspect it for instance-specific
wording and rewrite it at the appropriate reusable level. Apply this rule even
when the narrow wording or identifier appeared in source material, logs, test
output, a user prompt, or an existing artifact. Machine-required identifiers
may remain in source code, configuration, fixtures, commands, logs, and
datasets. Exact code identifiers, paths, and API names may be used in prose when
needed to locate the implementation, but descriptions of product or domain
behavior must remain reusable and non-identifying.

### Non-normative examples

These materially different examples illustrate the rule but do not define or
limit it:

- `ACT OI can form sawtooth` becomes
  `Monitored asset OI can form sawtooth`.
- `Export for account 42 stalls at 10,001 rows` becomes
  `Large exports can stall after crossing a pagination boundary`.
- `Add a redownload button for one customer's invoice` becomes
  `Allow an authorized customer to redownload a previously generated invoice`.
