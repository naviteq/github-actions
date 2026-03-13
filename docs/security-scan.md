# Security Scanners

This repository provides a thin orchestration layer for the existing security scanner workflows under `.github/workflows/`.

- CodeQL for static application security testing
- Dependency Review for pull request dependency change analysis
- Gitleaks for secret detection in Git history
- Trivy for container image or filesystem vulnerability scanning
- Checkov for Infrastructure-as-Code misconfiguration scanning

## Workflow File Tree

```text
.github/workflows/
  security-scan.yml
  security-scan-test.yml
  security-codeql.yml
  security-dependency-review.yml
  security-gitleaks.yml
  security-trivy.yml
  security-checkov.yaml
  security-codeql-test.yml
  security-dependency-review-test.yml
  security-gitleaks-test.yml
  security-trivy-test.yml
  security-checkov-test.yaml
```

## Included Workflows

### `security-scan.yml`

Top-level orchestration workflow for the existing security scan suite.

Supported triggers:

- `workflow_dispatch`
- `workflow_call`

This workflow fans out into dedicated jobs that call the reusable scanner workflows with `jobs.<job_id>.uses`.

### `security-scan-test.yml`

Runner workflow for integration verification.

Supported triggers:

- `workflow_call`
- `workflow_dispatch`

Use `workflow_dispatch` for manual runs from the Actions UI.

### `security-codeql.yml`

Reusable CodeQL workflow for source code analysis.

### `security-dependency-review.yml`

Reusable Dependency Review workflow for pull request dependency changes.

### `security-gitleaks.yml`

Reusable Gitleaks workflow for secret detection in repository history.

### `security-trivy.yml`

Reusable Trivy workflow for vulnerability scanning.

### `security-checkov.yaml`

Reusable Checkov workflow for Infrastructure-as-Code scanning.

## Failure Behavior

The baseline failure policy is:

- CodeQL fails when GitHub Code Scanning analysis fails or finds blocking issues according to GitHub's analyzer behavior
- Dependency Review fails on `high` or higher severity dependency changes
- Gitleaks fails when secrets are detected
- Trivy fails on `HIGH` or `CRITICAL` vulnerabilities
- Checkov fails on findings unless `soft_fail: true` is set

The orchestration workflow does not redefine scanner policies. It reuses the existing workflows as they are.

Note: Dependency Review only runs in the orchestration workflow for `pull_request`, `pull_request_target`, or `merge_group`, because the underlying action requires dependency diff context.

## How To Run The Full Suite

Manual run from GitHub Actions:

```yaml
name: Security / Scan Suite
```

Use `.github/workflows/security-scan.yml` and provide inputs required by the existing reusable workflows:

- `codeql_language`
- `runner`
- scanner-specific runner overrides such as `codeql_runner`, `trivy_runner`, and `checkov_runner`
- Trivy inputs when needed by the selected scan mode
- Checkov directory and frameworks when overriding the existing defaults
- `checkov_check`
- `checkov_skip_check`

For a manual verification run, trigger `.github/workflows/security-scan-test.yml`.
Its defaults are set for a practical smoke check:

- CodeQL: `python`
- Trivy image: `cgr.dev/chainguard/nginx:latest`
- Checkov directory: `fixtures/security-checkov`
- Checkov soft fail: `true`

## Reusable Usage Examples

Call the full suite:

```yaml
jobs:
  security:
    uses: ./.github/workflows/security-scan.yml
    with:
      codeql_language: python
      trivy_scan_type: image
      trivy_image_ref: cgr.dev/chainguard/nginx:latest
```

Call an individual scanner:

```yaml
jobs:
  trivy:
    uses: ./.github/workflows/security-trivy.yml
    with:
      scan_type: image
      image_ref: ghcr.io/example/app:latest
```

## Validation Structure And Test Data

Existing validation workflows and fixtures are preserved:

- CodeQL validation workflow: `.github/workflows/security-codeql-test.yml`
- Dependency Review validation workflow: `.github/workflows/security-dependency-review-test.yml`
- Gitleaks validation workflow: `.github/workflows/security-gitleaks-test.yml`
- Trivy validation workflow: `.github/workflows/security-trivy-test.yml`
- Checkov validation workflow: `.github/workflows/security-checkov-test.yaml`

Existing example fixtures:

- CodeQL fixture: `fixtures/security-codeql/sql_injection.py`
- Dependency Review fixture: `fixtures/security-dependency-review/package.json`
- Trivy fixture: `fixtures/security-trivy/Dockerfile.safe`
- Checkov fixtures: `fixtures/security-checkov/`

Examples with intentionally vulnerable code or misconfiguration are expected to fail the related validation workflow and serve as evidence that the scanner is wired correctly.

## PR Evidence Expectations

When updating these workflows, attach the following evidence to the pull request:

- link or screenshot of a successful manual `security-scan.yml` run
- link or screenshot of relevant reusable workflow runs
- failing validation run for an intentionally vulnerable fixture where applicable
- notes about any scanner-specific assumptions, such as the selected CodeQL language or Trivy scan mode
