# Helm Release Reusable Workflows

This setup is OCI-only for new pipelines.
It only packages and uploads Helm charts. It does not deploy to Kubernetes.

Legacy note:
- `.github/workflows/helm-release.yaml` is kept as-is.

## Design
- `.github/actions/helm-release-oci/action.yaml`: reusable OCI core action (common package/release logic).
- `.github/workflows/helm-release-github.yaml`: GHCR auth + OCI core action.
- `.github/workflows/helm-release-ecr.yaml`: AWS ECR auth + OCI core action.
- `.github/workflows/helm-release-gar.yaml`: GCP GAR auth + OCI core action.


## OCI Core Action Inputs
- `chart_path` (`string`, required): Path to Helm chart directory.
- `oci_registry` (`string`, required): OCI registry host.
- `oci_repo` (`string`, required): OCI repository path.
- `push_chart` (`boolean`, optional, default `true`): Push packaged chart to OCI registry.
- `bump_version_in_git` (`boolean`, optional, default `true`): Commit bumped chart version files to current branch.
- `lint_enabled` (`boolean`, optional, default `true`): Run `helm lint` before package.

Note:
- Version bump commit is intentionally tied to `push_chart=true`.
- Commit step runs only on branch refs and only when both `push_chart=true` and `bump_version_in_git=true`.

Outputs:
- `chart_name`
- `chart_version`
- `app_version`
- `package_file`
- `sha256`
- `upload_identifier`

## `.github/workflows/helm-release-github.yaml`
Inputs:
- `chart_path` (required)
- `ghcr_registry` (optional, default `ghcr.io`)
- `ghcr_repo` (required)
- `push_chart` (optional, default `true`)
- `bump_version_in_git` (optional, default `true`)
- `lint_enabled` (optional, default `true`)

Secrets:
- `token` (required when `push_chart=true`): token used for GHCR login.

Outputs:
- `chart_name`
- `chart_version`
- `app_version`
- `package_file`
- `sha256`
- `upload_identifier`

Caller permissions (minimum):
```yaml
permissions:
  contents: write
  packages: write
```

Example:
```yaml
jobs:
  release:
    uses: ./.github/workflows/helm-release-github.yaml
    with:
      chart_path: helm/museum
      ghcr_registry: ghcr.io
      ghcr_repo: my-org/helm-charts
      push_chart: true
      bump_version_in_git: true
    secrets:
      token: ${{ secrets.GITHUB_TOKEN }}
```

## `.github/workflows/helm-release-ecr.yaml`
Inputs:
- `chart_path` (required)
- `ecr_registry` (required)
- `ecr_repo` (required)
- `aws_region` (required when `push_chart=true`)
- `aws_role_to_assume` (required when `push_chart=true`)
- `push_chart` (optional, default `true`)
- `bump_version_in_git` (optional, default `true`)
- `lint_enabled` (optional, default `true`)

Secrets:
- `token` (optional): git token for checkout/commit push.

Outputs:
- `chart_name`
- `chart_version`
- `app_version`
- `package_file`
- `sha256`
- `upload_identifier`

Caller permissions (minimum):
```yaml
permissions:
  contents: write
  id-token: write
```

Example:
```yaml
jobs:
  release:
    uses: ./.github/workflows/helm-release-ecr.yaml
    with:
      chart_path: helm/museum
      ecr_registry: 123456789012.dkr.ecr.eu-west-1.amazonaws.com
      ecr_repo: museum/charts
      aws_region: eu-west-1
      aws_role_to_assume: arn:aws:iam::123456789012:role/github-actions-helm-release
      push_chart: true
      bump_version_in_git: true
```

## `.github/workflows/helm-release-gar.yaml`
Inputs:
- `chart_path` (required)
- `gar_registry` (required)
- `gar_repo` (required)
- `gcp_workload_identity_provider` (required when `push_chart=true`)
- `gcp_service_account` (required when `push_chart=true`)
- `push_chart` (optional, default `true`)
- `bump_version_in_git` (optional, default `true`)
- `lint_enabled` (optional, default `true`)

Secrets:
- none

Outputs:
- `chart_name`
- `chart_version`
- `app_version`
- `package_file`
- `sha256`
- `upload_identifier`

Caller permissions (minimum):
```yaml
permissions:
  contents: write
  id-token: write
```

Example:
```yaml
jobs:
  release:
    uses: ./.github/workflows/helm-release-gar.yaml
    with:
      chart_path: helm/museum
      gar_registry: europe-west1-docker.pkg.dev
      gar_repo: my-project/my-repo
      gcp_workload_identity_provider: projects/123456789/locations/global/workloadIdentityPools/pool/providers/provider
      gcp_service_account: gha-helm@my-project.iam.gserviceaccount.com
      push_chart: true
      bump_version_in_git: true
```
