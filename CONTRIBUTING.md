# Contributing

This repository is a mirror. The workflows and actions in it are developed in
`naviteq-ltd/github-workflows`, a private Naviteq repository, and copied here on every
release (see [README](README.md#how-a-release-reaches-this-repository)). That decides
where each kind of contribution goes.

## Issues

Open them here: bugs, missing inputs, behaviour you did not expect, ideas. A useful
report names the workflow or action, the release tag or SHA you pinned, the inputs you
passed, and the failing part of the run log with secrets removed. Maintainers carry the
work over to the source repository and link the release that fixes it back to the issue.

Do not report a security vulnerability in a public issue. Contact Naviteq through
[naviteq.io](https://naviteq.io) and ask for the security contact instead.

## Pull requests

What happens to a pull request depends on the files it touches.

**Mirrored files: `.github/workflows/*` and `.github/actions/*`.** Do not expect these
to be merged here. The next release copies the source repository over them, so a change
merged only in this repository would disappear. Open the pull request anyway if code
explains the change better than an issue does: a maintainer re-does it in the source
repository, with a test, and closes your pull request with a link to the release that
carries it. Keep it to one change, and say in the description what problem it solves.

**Files that belong to this repository.** These are not mirrored, so pull requests
against them are reviewed and merged here:

- `README.md`, `CONTRIBUTING.md`
- `.github/workflows/release.yml`, which tags merged mirror pull requests
- `.github/workflows/1-checkov.yaml` and `.github/actionlint.yaml`, this repository's own
  pull request checks
- `.pre-commit-config.yaml`, `scripts/`, `renovate.json`, `.gitignore`

`.github/workflows/create-release.yaml` is a leftover: the source repository stopped
mirroring it, and the copy here stays for the public repositories that still call it.
Treat a change to it like a change to the files above.

### Requirements

- Commits must be signed. The branch ruleset rejects unsigned commits.
- Use a [Conventional Commits](https://www.conventionalcommits.org/) title, for example
  `docs: explain the Renovate version cap`.
- One approving review is required. Mirror pull requests are merged with a merge commit,
  and squash merging is disabled: `release.yml` only runs on a merge commit, because a
  squash commit inherits the `[skip ci]` marker of the mirror branch.

## For Naviteq engineers

Feature work, fixes and dependency updates go to `naviteq-ltd/github-workflows`. Follow
its `AGENTS.md` and `CLAUDE.md`; the short version:

1. Branch from `main` in the source repository, add or update the `z-test-*` workflow
   that covers the change, and open a pull request. Workflows and actions that only work
   inside Naviteq (internal environments, org-only secrets, scripts under `scripts/`)
   must be added to the mirror's exclude globs in `mirror.yaml`; see `docs/mirror.md`
   there.
2. After the merge, release-please opens a release pull request. Merging it publishes
   `vX.Y.Z`, and `mirror.yaml` opens `mirror/vX.Y.Z` here.
3. Review the mirror pull request (the body is the changelog) and merge it with a merge
   commit. `release.yml` tags it `vX.Y.Z` and publishes the release. If a squash merge
   slipped through, dispatch `release.yml` with the merge commit as `sha`.

Do not edit mirrored files here to fix something quickly. Fix it in the source
repository and release; the next mirror run would overwrite the local fix.

A pull request opened here against a mirrored file is ported the same way: re-implement
it in the source repository with a test, then close it here with a link to the source
pull request and, once out, the release.

To test an unreleased change from a public repository, sync it to a `test/*` branch here
and pin the consumer to that commit for the duration of the test. Never merge a
`test/*` branch or a `mirror/preview-*` pull request into `main`.
