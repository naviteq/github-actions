# Actionlint Test

This repository validates its GitHub Actions workflows with `actionlint` in CI.

- CI workflow: `.github/workflows/actionlint.yaml`
- Test workflow: `.github/workflows/actionlint-test.yaml`
- Invalid fixtures: `fixtures/actionlint/invalid-workflow.yaml` and `fixtures/actionlint/invalid-local-action.yaml`

## What gets checked

- All workflow files in `.github/workflows/`
- Composite action definitions in `.github/actions/**/action.yaml`
- Custom self-hosted runner labels declared in `.github/actionlint.yaml`

## CI behavior

- `pull_request`: `reviewdog/action-actionlint` reports workflow findings as PR checks
- `push` to `main`: `devops-actions/actionlint` runs workflow linting and fails the job on findings
- `bettermarks/composite-action-lint` validates local composite actions in both cases
- `actionlint-test.yaml` is a `workflow_dispatch` and `workflow_call` proof job that replaces checked out files with invalid fixtures and asserts both linters fail
- `create-release.yaml` is intentionally not covered by the functional framework because it is a destructive release workflow
