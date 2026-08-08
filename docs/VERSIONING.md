# Versioning and distribution

`perp-md` uses strict Semantic Versions and exposes its runtime version through
installed package metadata. A release tag is `vX.Y.Z` and must match
`project.version` exactly.

GitHub release assets and PyPI are distribution boundaries. The GitHub release
workflow builds a universal wheel and source distribution from the tagged tree,
verifies and attests them, and attaches them to the release. The PyPI workflow
independently checks out the exact tag, verifies that it is merged to `main` and
matches `project.version`, builds and checks fresh distributions in a job
without OIDC privileges, and publishes them from a separate `pypi` environment
through PyPI Trusted Publishing with attestations.

New `vX.Y.Z` tags trigger both release workflows. To publish an existing GitHub
release after its PyPI trusted publisher is configured, dispatch
`publish-pypi.yml` with that exact tag. Manual dispatch accepts only strict
release tags, requires an existing non-draft GitHub release, and refuses a
version already present on PyPI. Published GitHub or PyPI artifacts are never
replaced; corrections receive a new patch version.

## Trusted publisher identity

The repository `pypi` environment permits only strict release-tag runs and
manual dispatches from `main`. It stores no package-index credential. The PyPI
pending publisher must use these exact identity values:

- PyPI project name: `perp-md`
- GitHub owner: `latheiere`
- GitHub repository: `perp-md`
- Workflow filename: `publish-pypi.yml`
- Environment name: `pypi`
