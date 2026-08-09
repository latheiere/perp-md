# perp-md

[![PyPI](https://img.shields.io/pypi/v/perp-md)](https://pypi.org/project/perp-md/)
[![Python](https://img.shields.io/pypi/pyversions/perp-md)](https://pypi.org/project/perp-md/)
[![CI](https://github.com/latheiere/perp-md/actions/workflows/ci.yml/badge.svg)](https://github.com/latheiere/perp-md/actions/workflows/ci.yml)

Typed, asynchronous acquisition of perpetual-market open interest and funding
for Python applications.

`perp-md` turns provider-independent
[CDM instrument references](https://github.com/latheiere/crypto-derivative-markets)
into source-faithful observations. It keeps provider protocols, identity
requirements, pagination, and acquisition evidence behind a small public API.

## Install

```sh
pip install perp-md
```

Optional CCXT fallback adapters are available through the `ccxt` extra:

```sh
pip install "perp-md[ccxt]"
```

## Quick start

Pass the CDM reference supplied by your instrument catalog directly to a
client:

```python
from cdm import InstrumentReferenceV1
from perp_md import OpenInterestClient


async def current_open_interest(
    provider_id: str,
    reference: InstrumentReferenceV1,
):
    async with OpenInterestClient() as client:
        result = await client.fetch_reference(
            provider_id,
            reference,
            include_history=False,
        )
        return result.current
```

`FundingClient` provides the corresponding funding workflow. Both clients can
assess runtime support and return structured metadata or identity gaps before
acquisition. Observations retain native values, timestamps, calculation
lineage, and normalization evidence; missing data is never represented as
zero.

## Scope

The library owns market-data acquisition: provider protocols, operation-level
identity requirements, source validation, bounded history retrieval, and
declared adapter coverage. It does not discover instruments, persist data,
schedule jobs, calculate application metrics, or expose a service API.

See the [public contract](https://github.com/latheiere/perp-md/blob/main/docs/CONTRACT.md)
for data and error semantics, the
[architecture guide](https://github.com/latheiere/perp-md/blob/main/docs/ARCHITECTURE.md)
for module boundaries, and the
[changelog](https://github.com/latheiere/perp-md/blob/main/CHANGELOG.md) for
release notes. `perp-md-coverage` emits the deterministic declared-coverage
manifest for documentation and capability tooling.

## Status and support

The API is alpha, follows Semantic Versioning, and supports Python 3.11–3.13.
Use [GitHub Issues](https://github.com/latheiere/perp-md/issues) for defects and
feature requests, and [private security advisories](https://github.com/latheiere/perp-md/security/advisories/new)
for vulnerabilities. Licensed under Apache-2.0.
