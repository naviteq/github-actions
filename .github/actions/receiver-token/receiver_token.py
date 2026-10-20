#!/usr/bin/env python3
"""Write the Console's health-receiver token into an existing AWS Secrets Manager entry.

Best effort: every path exits 0 and a refused write only warns. The token is masked before
anything is printed, reaches the AWS CLI only through a private temporary file, and the
CLI's own output is discarded. The entry itself is created by the environment's Terraform,
so only put-secret-value is called. It lives beside its action because the public mirror
copies actions only. → docs/terragrunt-apply-fresh.md

    RECEIVER_TOKEN=... SECRET_ID=/<cluster>/argocd/runestra-receiver-token AWS_REGION=eu-west-1 \\
        python3 .github/actions/receiver-token/receiver_token.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
SECRET_ID_RE = re.compile(r"^/[A-Za-z0-9/_.-]+$")
AWS_TIMEOUT = 120


def warn(reason: str) -> None:
    print(f"::warning title=receiver-token::{reason}", flush=True)


def mask(token: str) -> None:
    """One add-mask per line: a workflow command ends at a newline, so the rest would print."""
    for line in token.splitlines():
        if line.strip():
            print(f"::add-mask::{line}", flush=True)


def skip_reason(token: str, secret_id: str) -> str | None:
    if not TOKEN_RE.match(token):
        return "Receiver token skipped: expected 32-128 characters of A-Za-z0-9_-"
    if not SECRET_ID_RE.match(secret_id):
        return "Receiver token skipped: secret id must start with / and use only A-Za-z0-9/_.-"
    if shutil.which("aws") is None or not os.environ.get("AWS_REGION"):
        return "Receiver token skipped: no aws CLI or no AWS region configured for this run"
    return None


def put(token: str, secret_id: str) -> bool:
    """True when Secrets Manager took the new value."""
    handle, path = tempfile.mkstemp(prefix="receiver-token-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(token)
        result = subprocess.run(
            ["aws", "secretsmanager", "put-secret-value", "--secret-id", secret_id,
             "--secret-string", f"file://{path}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=AWS_TIMEOUT, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError:
        pass


def main() -> int:
    token = os.environ.get("RECEIVER_TOKEN", "")
    secret_id = os.environ.get("SECRET_ID", "")
    try:
        mask(token)
        if not token or not secret_id:
            print("Receiver token skipped: no token or no secret id", flush=True)
            return 0
        reason = skip_reason(token, secret_id)
        if reason:
            warn(reason)
            return 0
        if put(token, secret_id):
            print("Receiver token stored", flush=True)
            summary("Receiver token stored")
        else:
            warn("Receiver token was not stored: Secrets Manager refused the write; "
                 "the entry is created by the environment's Terraform")
    except Exception:  # noqa: BLE001 - best effort, never fail the apply
        warn("Receiver token was not stored: unexpected error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
