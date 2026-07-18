# Contributing

Changes must preserve the public contract and application-neutral boundary.
Run the offline suite and artifact checks before proposing a release:

```sh
python -m pytest -q
python -m build
python -m twine check dist/*
```

Do not add credentials, private endpoints, consumer schemas, concrete market
identities, or network-dependent tests.
