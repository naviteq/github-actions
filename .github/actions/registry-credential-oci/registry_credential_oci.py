#!/usr/bin/env python3
"""Store the Console-issued registry credential in the environment's OCI Vault.

Best effort: every path exits 0 and a refused write only warns. Both values are masked before
anything is printed and reach the oci CLI only through a private temporary file; the CLI's own
output is captured and never printed. Nothing else creates this secret, so it is created when
absent, updated when its value differs, left alone when equal (no new secret version) and never
touched when not ACTIVE. It lives beside its action because the public mirror copies actions only.
→ docs/terragrunt-apply-fresh.md

    CLIENT_ID=... CLIENT_SECRET=... SECRET_NAME=naviteq-registry VAULT_OCID=ocid1.vault... \\
        OCI_REGION=eu-frankfurt-1 GITHUB_REPOSITORY=owner/repo \\
        python3 .github/actions/registry-credential-oci/registry_credential_oci.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys

# Explicit: an isolated interpreter (python3 -I) does not put the script's directory on the path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oci_vault  # noqa: E402

TITLE = "registry-credential-oci"
SUBJECT = "Registry credential"
DESCRIPTION = "Runestra registry client credential, written by the Runestra run"


def same_credential(current: str, payload: str) -> bool:
    """True when the stored value already holds this credential, however it is formatted."""
    try:
        return json.loads(current) == json.loads(payload)
    except ValueError:
        return False


def skip_reason(secret_name: str, region: str) -> str | None:
    if not oci_vault.valid_secret_name(secret_name):
        return ("Registry credential skipped: an OCI Vault secret name is flat, 1-255 characters of "
                "A-Za-z0-9._-")
    if not oci_vault.valid_region(region):
        return "Registry credential skipped: the region is not an OCI region identifier"
    if shutil.which("oci") is None:
        return "Registry credential skipped: no oci CLI on this runner"
    return None


def main() -> int:
    client_id = os.environ.get("CLIENT_ID", "")
    client_secret = os.environ.get("CLIENT_SECRET", "")
    secret_name = os.environ.get("SECRET_NAME", "")
    region = os.environ.get("OCI_REGION", "")
    try:
        oci_vault.mask(client_id)
        oci_vault.mask(client_secret)
        if not client_id or not client_secret or not secret_name or not region:
            oci_vault.report("Registry credential skipped: no credential, no secret name or no region")
            return 0
        reason = skip_reason(secret_name, region)
        if reason:
            oci_vault.warn(reason, TITLE)
            return 0
        vault = oci_vault.resolve_vault_ocid(os.environ.get("VAULT_OCID", ""), os.environ.get("VAULT_UNIT_DIR", ""),
                                             title=TITLE)
        if vault is None:
            return 0
        payload = json.dumps({"client_id": client_id, "client_secret": client_secret}, separators=(",", ":"))
        outcome = oci_vault.store_secret(secret_name, payload, same_credential, os.environ.get("GITHUB_REPOSITORY", ""),
                                         vault, region, subject=SUBJECT, title=TITLE, description=DESCRIPTION)
        if outcome != "skipped":
            oci_vault.report(f"Registry credential {outcome} in OCI Vault: {secret_name}")
    except Exception as error:  # noqa: BLE001 - best effort, never fail the apply
        oci_vault.warn(f"Registry credential was not stored: unexpected {type(error).__name__}", TITLE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
