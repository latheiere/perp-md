# Versioning and distribution

`perp-md` uses strict Semantic Versions and exposes its runtime version through
installed package metadata. A release tag is `vX.Y.Z` and must match
`project.version` exactly.

GitHub release assets are the distribution boundary. CI builds a universal
wheel and source distribution from the tagged tree, verifies them, and attaches
them to the release. Published assets are never replaced; corrections receive
a new patch version. Direct installations use a versioned wheel URL and its
SHA-256 digest.
