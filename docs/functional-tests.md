# Functional Tests

This repository runs functional tests for GitHub Actions and workflows in CI.

CI workflow:
- `.github/workflows/functional-tests.yml`

Test entrypoints:
- `.github/workflows/actionlint-test.yaml`
- `.github/workflows/docker-build-test.yaml`
- `.github/workflows/helm-release-oci-test.yaml`
- `.github/workflows/security-checkov-test.yaml`
- `.github/workflows/security-codeql-test.yml`
- `.github/workflows/security-dependency-review-test.yml`
- `.github/workflows/security-gitleaks-test.yml`
- `.github/workflows/security-scan-test.yml`
- `.github/workflows/security-trivy-test.yml`

## How it works

- On `pull_request` and `push` to `main`, `functional-tests.yml` detects changed workflow and action files with `dorny/paths-filter`.
- Only the matching functional test workflows are executed.
- `security-scan-test.yml` is used as the suite-level orchestration smoke test for `security-scan.yml`.

## Onboarding a new workflow or action

1. Add or update a dedicated `...-test.yml` workflow under `.github/workflows/`.
2. Give the workflow a standard name in the format `<workflow name> TEST`.
3. Add at least one success-path scenario.
4. Add a failure-path scenario only when it can be asserted without making the whole workflow fail.
5. Register the new component and its test entrypoint in `.github/workflows/functional-tests.yml` when it should run automatically.

## Notes

- Internal reusable workflow tests use local paths, and the Helm OCI core action is exercised through a direct local harness, so PR runs execute the changed branch code.
- `create-release.yaml` is intentionally excluded from the automatic functional framework because it creates tags and releases; that behavior should be validated in a dedicated sandbox repository if needed.
- `security-dependency-review.yml` only executes its real dependency review path for pull request events because the upstream action requires PR diff context.
