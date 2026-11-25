#!/usr/bin/env python3
"""Store the Console's health-receiver token in the environment's OCI Vault.

Best effort: every path exits 0 and a refused write only warns. The token is masked before anything
is printed and reaches the oci CLI only through the shared module's private temporary file. No
Terraform creates this secret on OCI, so it is created when absent, updated when the stored string
differs, left alone when equal (no new secret version) and never touched when not ACTIVE. The OCI
mechanics live in ../registry-credential-oci/oci_vault.py; the public mirror copies both actions.
→ docs/terragrunt-apply-fresh.md

    RECEIVER_TOKEN=... SECRET_NAME=<cluster>-argocd-runestra-receiver-token VAULT_OCID=ocid1.vault... \\
        OCI_REGION=eu-frankfurt-1 GITHUB_REPOSITORY=owner/repo \\
        python3 .github/actions/receiver-token-oci/receiver_token_oci.py
"""

from __future__ import annotations

import os
import re
import shutil
import sys

# Explicit: an isolated interpreter (python3 -I) does not put the script's directory on the path.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "registry-credential-oci"))
import oci_vault  # noqa: E402

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
TITLE = "receiver-token-oci"
SUBJECT = "Receiver token"
DESCRIPTION = "Runestra Console health-receiver token, written by the Runestra run"


def same_token(current: str, token: str) -> bool:
    return current == token


def skip_reason(token: str, secret_name: str, region: str) -> str | None:
    if not TOKEN_RE.fullmatch(token):
        return "Receiver token skipped: expected 32-128 characters of A-Za-z0-9_-"
    if not oci_vault.valid_secret_name(secret_name):
        return "Receiver token skipped: an OCI Vault secret name is flat, 1-255 characters of A-Za-z0-9._-"
    if not oci_vault.valid_region(region):
        return "Receiver token skipped: the region is not an OCI region identifier"
    if shutil.which("oci") is None:
        return "Receiver token skipped: no oci CLI on this runner"
    return None


def main() -> int:
    token = os.environ.get("RECEIVER_TOKEN", "")
    secret_name = os.environ.get("SECRET_NAME", "")
    region = os.environ.get("OCI_REGION", "")
    try:
        oci_vault.mask(token)
        if not token or not secret_name or not region:
            oci_vault.report("Receiver token skipped: no token, no secret name or no region")
            return 0
        reason = skip_reason(token, secret_name, region)
        if reason:
            oci_vault.warn(reason, TITLE)
            return 0
        vault = oci_vault.resolve_vault_ocid(os.environ.get("VAULT_OCID", ""), os.environ.get("VAULT_UNIT_DIR", ""),
                                             title=TITLE)
        if vault is None:
            return 0
        outcome = oci_vault.store_secret(secret_name, token, same_token, os.environ.get("GITHUB_REPOSITORY", ""),
                                         vault, region, subject=SUBJECT, title=TITLE, description=DESCRIPTION)
        if outcome != "skipped":
            oci_vault.report(f"Receiver token {outcome} in OCI Vault: {secret_name}")
            oci_vault.set_output("result", outcome)
    except Exception as error:  # noqa: BLE001 - best effort, never fail the apply
        oci_vault.warn(f"Receiver token was not stored: unexpected {type(error).__name__}", TITLE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
