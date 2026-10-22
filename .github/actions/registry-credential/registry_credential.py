#!/usr/bin/env python3
"""Store the Console-issued registry credential in the client's AWS Secrets Manager.

Best effort: every path exits 0 and a refused write only warns. Both values are masked before
anything is printed and reach the AWS CLI only through a private temporary file; the CLI's own
output is discarded, or captured and never printed. Nothing else creates this secret, so it is
created when absent, overwritten when its value differs and left alone when equal. It lives beside
its action because the public mirror copies actions only. → docs/terragrunt-apply-fresh.md

    CLIENT_ID=... CLIENT_SECRET=... SECRET_ID=/naviteq/registry GITHUB_REPOSITORY=owner/repo \\
        AWS_REGION=eu-west-1 python3 .github/actions/registry-credential/registry_credential.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

SECRET_ID_RE = re.compile(r"^/[A-Za-z0-9/_.+=@-]+$")
AWS_TIMEOUT = 120
MANAGED_BY_TAG = "runestra:managed-by"
MANAGED_BY = "runestra-run"
REPOSITORY_TAG = "runestra:repository"


def warn(reason: str) -> None:
    print(f"::warning title=registry-credential::{reason}", flush=True)


def mask(value: str) -> None:
    """One add-mask per line: a workflow command ends at a newline, so the rest would print."""
    for line in value.splitlines():
        if line.strip():
            print(f"::add-mask::{line}", flush=True)


def summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError:
        pass


def report(line: str) -> None:
    print(line, flush=True)
    summary(line)


def skip_reason(secret_id: str) -> str | None:
    if not SECRET_ID_RE.match(secret_id):
        return "Registry credential skipped: secret id must start with / and use only A-Za-z0-9/_.+=@-"
    if shutil.which("aws") is None or not os.environ.get("AWS_REGION"):
        return "Registry credential skipped: no aws CLI or no AWS region configured for this run"
    return None


def aws(args: list[str]) -> subprocess.CompletedProcess | None:
    """Run a Secrets Manager call with its output captured; None when the CLI did not run."""
    try:
        return subprocess.run(
            ["aws", "secretsmanager", *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=AWS_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def describe(secret_id: str) -> tuple[str, dict[str, str]]:
    """("absent", {}), ("present", tags) or ("unknown", {}) when the secret cannot be read."""
    result = aws(["describe-secret", "--secret-id", secret_id, "--output", "json"])
    if result is None:
        return "unknown", {}
    if result.returncode != 0:
        return ("absent" if "ResourceNotFoundException" in result.stderr else "unknown"), {}
    try:
        tags = json.loads(result.stdout).get("Tags") or []
        return "present", {str(tag.get("Key")): str(tag.get("Value", "")) for tag in tags}
    except (ValueError, AttributeError):
        return "present", {}


def unchanged(secret_id: str, expected: dict[str, str]) -> bool:
    """True when the stored value already holds this credential, however it is formatted."""
    result = aws(["get-secret-value", "--secret-id", secret_id, "--query", "SecretString", "--output", "text"])
    if result is None or result.returncode != 0:
        return False
    try:
        return json.loads(result.stdout) == expected
    except ValueError:
        return False


def write(args: list[str], payload: str) -> bool:
    """Run a create or put with the payload in a mode-0600 file; True when it succeeded."""
    handle, path = tempfile.mkstemp(prefix="registry-credential-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(payload)
        result = subprocess.run(
            ["aws", "secretsmanager", *args, "--secret-string", f"file://{path}"],
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


def tag_list(repository: str) -> str:
    tags = [{"Key": MANAGED_BY_TAG, "Value": MANAGED_BY}]
    if repository:
        tags.append({"Key": REPOSITORY_TAG, "Value": repository})
    return json.dumps(tags, separators=(",", ":"))


def store(secret_id: str, credential: dict[str, str], repository: str) -> None:
    payload = json.dumps(credential, separators=(",", ":"))
    state, tags = describe(secret_id)
    if state == "unknown":
        warn(f"Registry credential was not stored: could not read {secret_id} in Secrets Manager")
        return
    if state == "absent":
        if write(["create-secret", "--name", secret_id, "--tags", tag_list(repository)], payload):
            report("Registry credential created")
        else:
            warn(f"Registry credential was not stored: Secrets Manager refused to create {secret_id}")
        return
    if unchanged(secret_id, credential):
        report("Registry credential unchanged")
        return
    if not write(["put-secret-value", "--secret-id", secret_id], payload):
        warn(f"Registry credential was not stored: Secrets Manager refused the write to {secret_id}")
        return
    report("Registry credential updated")
    previous = tags.get(REPOSITORY_TAG, "")
    if previous and repository and previous != repository:
        line = (f"Registry credential at {secret_id} was last written by another repository ({previous}); "
                "overwritten with this repository's credential")
        warn(line)
        summary(line)
    tagged = aws(["tag-resource", "--secret-id", secret_id, "--tags", tag_list(repository)])
    if tagged is None or tagged.returncode != 0:
        warn(f"Registry credential stored, but {secret_id} could not be tagged")


def main() -> int:
    client_id = os.environ.get("CLIENT_ID", "")
    client_secret = os.environ.get("CLIENT_SECRET", "")
    secret_id = os.environ.get("SECRET_ID", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        mask(client_id)
        mask(client_secret)
        if not client_id or not client_secret or not secret_id:
            report("Registry credential skipped: no credential or no secret id")
            return 0
        reason = skip_reason(secret_id)
        if reason:
            warn(reason)
            return 0
        store(secret_id, {"client_id": client_id, "client_secret": client_secret}, repository)
    except Exception:  # noqa: BLE001 - best effort, never fail the apply
        warn("Registry credential was not stored: unexpected error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
