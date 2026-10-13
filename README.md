# github-actions

Reusable GitHub Actions workflows and composite actions from [Naviteq](https://naviteq.io).

This repository is the public mirror of `naviteq-ltd/github-workflows`, the private
repository where Naviteq develops and tests them. Every release there is copied here as
a pull request, merged, and tagged with the same version. See
[How a release reaches this repository](#how-a-release-reaches-this-repository).

## What is here

### Reusable workflows

Call these with `jobs.<id>.uses`. Inputs, secrets and outputs are described in each
file's `on.workflow_call` block.

| Area | Workflows |
|---|---|
| Pull request hygiene | [`pr-lint`](.github/workflows/pr-lint.yaml) (Conventional Commits pull request title), [`pre-commit`](.github/workflows/pre-commit.yaml), [`actionlint`](.github/workflows/actionlint.yaml) |
| Security scanning | [`security-scan`](.github/workflows/security-scan.yaml) runs the five scanners below in one call: [`security-checkov`](.github/workflows/security-checkov.yaml), [`security-codeql`](.github/workflows/security-codeql.yaml), [`security-dependency-review`](.github/workflows/security-dependency-review.yaml), [`security-gitleaks`](.github/workflows/security-gitleaks.yaml), [`security-trivy`](.github/workflows/security-trivy.yaml) |
| Container images | [`docker-build`](.github/workflows/docker-build.yaml): BuildKit build and push, optional native per-platform legs, Trivy scan, SBOM |
| Helm charts | Package and push a chart to an OCI registry: [`helm-release-ecr`](.github/workflows/helm-release-ecr.yaml), [`helm-release-gar`](.github/workflows/helm-release-gar.yaml), [`helm-release-github`](.github/workflows/helm-release-github.yaml), [`helm-release-ocir`](.github/workflows/helm-release-ocir.yaml) |
| Terragrunt | [`terragrunt-validate-lint`](.github/workflows/terragrunt-validate-lint.yaml), [`terragrunt-plan`](.github/workflows/terragrunt-plan.yaml) (plan on a pull request, with a summary comment), [`terragrunt-apply-gate`](.github/workflows/terragrunt-apply-gate.yaml) and [`terragrunt-apply`](.github/workflows/terragrunt-apply.yaml) (apply the saved plans after an approval issue), [`terragrunt-destroy`](.github/workflows/terragrunt-destroy.yaml), [`terragrunt-drift`](.github/workflows/terragrunt-drift.yaml) |
| Account cleanup | [`aws-nuke-plan`](.github/workflows/aws-nuke-plan.yaml), [`aws-nuke-parse`](.github/workflows/aws-nuke-parse.yaml), [`aws-nuke-apply`](.github/workflows/aws-nuke-apply.yaml), and the same three for OCI: [`oci-nuke-plan`](.github/workflows/oci-nuke-plan.yaml), [`oci-nuke-parse`](.github/workflows/oci-nuke-parse.yaml), [`oci-nuke-apply`](.github/workflows/oci-nuke-apply.yaml). Plan, approve by comment, apply. |
| Deployment | [`deploy-ecs`](.github/workflows/deploy-ecs.yaml) |

### Composite actions

Call these with `steps[*].uses`. Inputs are described in each `action.yaml`.

| Area | Actions |
|---|---|
| Tool installation | [`ensure-tool`](.github/actions/ensure-tool) keeps a tool already on the runner when it is exactly the requested version, otherwise downloads it and verifies the checksum. Built on it: [`setup-terragrunt`](.github/actions/setup-terragrunt), [`setup-kubectl`](.github/actions/setup-kubectl), [`setup-checkov`](.github/actions/setup-checkov), [`setup-oci-cli`](.github/actions/setup-oci-cli), [`setup-boilerplate`](.github/actions/setup-boilerplate), [`setup-aws-cli`](.github/actions/setup-aws-cli), [`setup-yq`](.github/actions/setup-yq), [`aws-nuke-install`](.github/actions/aws-nuke-install). [`oci-nuke-install`](.github/actions/oci-nuke-install) installs oci-nuke from a release tag or builds it from a commit |
| Cloud credentials | [`configure-oci-credentials`](.github/actions/configure-oci-credentials) exchanges the job's GitHub OIDC token for a short-lived OCI session, the OCI counterpart of `aws-actions/configure-aws-credentials` |
| Terragrunt building blocks | The steps the Terragrunt workflows are made of, usable on their own: [`terragrunt-select-units`](.github/actions/terragrunt-select-units), [`terragrunt-plan-profiles`](.github/actions/terragrunt-plan-profiles), [`terragrunt-warm-providers`](.github/actions/terragrunt-warm-providers), [`terragrunt-oci-session`](.github/actions/terragrunt-oci-session), [`terragrunt-plan-report`](.github/actions/terragrunt-plan-report), [`terragrunt-plan-cost`](.github/actions/terragrunt-plan-cost), [`terragrunt-plan-result`](.github/actions/terragrunt-plan-result), [`terragrunt-apply-gate`](.github/actions/terragrunt-apply-gate), [`terragrunt-apply-units`](.github/actions/terragrunt-apply-units), [`terragrunt-drift-issues`](.github/actions/terragrunt-drift-issues) |
| Other | [`helm-release-oci`](.github/actions/helm-release-oci) (registry-agnostic part of the `helm-release-*` workflows), [`action-lint`](.github/actions/action-lint) (actionlint for composite actions) |

Comments in the workflow files sometimes point at `docs/<name>.md`. Those documents live
in the private source repository and are not published here; the input descriptions are
written to stand on their own.

## Usage

Pin every reference to the full commit SHA of a release tag and keep the tag in a
comment, so Renovate or Dependabot can read and bump it:

```yaml
jobs:
  security:
    uses: naviteq/github-actions/.github/workflows/security-scan.yaml@88b07ffdf6c34e2c890377e905e45a46b50f1251 # v1.70.2
    permissions:
      actions: read
      contents: read
      pull-requests: read
      security-events: write
```

```yaml
steps:
  - uses: naviteq/github-actions/.github/actions/ensure-tool@88b07ffdf6c34e2c890377e905e45a46b50f1251 # v1.70.2
    with:
      tool: helm
      version: 3.19.0
```

Things worth knowing before the first run:

- **Pin the tagged commit, not a branch.** `main` moves on every release, and `test/*`
  branches carry unreleased builds Naviteq uses for its own testing. Some workflows call
  other workflows in this repository (`terragrunt-drift` calls `terragrunt-plan`); the
  tagged commit is the one whose nested references are guaranteed to resolve.
- **Grant the permissions the workflow asks for.** A reusable workflow cannot raise its
  token above what the calling job grants. Each job in the called file declares its
  `permissions:` block; copy it into the calling job. Cloud access goes through GitHub
  OIDC (`id-token: write`), not long-lived keys.
- **Runners.** Most workflows take a `runner` input: a single label or a JSON array of
  labels, defaulting to `ubuntu-24.04`.
- **Naviteq-specific secrets are optional.** Some workflows accept secrets such as a
  GitHub App key or a Runestra registry client ID for resolving private sources. Leave
  them unset if you do not need private modules or charts.

### Renovate

This repository also carries tags from before it became a mirror: `v0.x` to `v1.20.9`
and `v2.0.0` to `v2.0.3`. The `v2.0.x` tags are older than every mirrored release
from `v1.40.0` on, so cap the version or Renovate will offer them as a major upgrade:

```json
{
  "packageRules": [
    {
      "matchDepNames": ["naviteq/github-actions"],
      "allowedVersions": "<2.0.0"
    }
  ]
}
```

## How a release reaches this repository

1. A change is merged into `naviteq-ltd/github-workflows` and released there by
   release-please as `vX.Y.Z`.
2. The source repository opens a pull request here from the branch `mirror/vX.Y.Z`. It
   copies `.github/workflows` and `.github/actions`, leaves out everything that only works
   inside Naviteq (self-CI, internal environments, release tooling), rewrites
   `naviteq-ltd/github-workflows` references to `naviteq/github-actions`, and re-pins
   them to commits that exist in this repository. The pull request body is the release
   changelog.
3. A maintainer reviews the pull request and merges it with a merge commit.
4. [`release.yml`](.github/workflows/release.yml) tags the merge commit `vX.Y.Z` and
   publishes a GitHub release with the same notes.

Each mirror run also creates a `mirror-pins/vX.Y.Z` tag. It keeps the
intermediate re-pin commits reachable, which nested workflow calls need. Do not pin to
it; it exists for GitHub, not for you.

## Contributing

Issues are welcome here. Code changes to the mirrored workflows and actions are made in
the private source repository, so a pull request here is treated as a proposal and
re-done there. [CONTRIBUTING.md](CONTRIBUTING.md) explains what that means for you and
which files in this repository accept pull requests directly.
