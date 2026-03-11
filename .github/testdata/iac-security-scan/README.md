This directory contains intentionally insecure Infrastructure-as-Code examples used to validate the Checkov GitHub Actions security scanning workflow.

The files in this directory are test data only and must not be used in real environments.

The CI pipeline is expected to FAIL when Checkov scans this directory because the Terraform, Kubernetes, and Helm examples are deliberately misconfigured to trigger security violations.
