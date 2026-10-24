#!/usr/bin/env python3
"""Delete the OpenTofu state locks a killed run left behind under one environment's state prefix.

Best effort: every path exits 0 and a refused call only warns. A run that is killed outright never
releases its S3 native lock, so the next run fails every unit on `Error acquiring the state lock`.
The sweep covers the regional state bucket and the global one, where units under `global/` keep
their state, and only the prefix of the working directory. It is safe only where nothing else can
hold a lock on that prefix. The CLI's output is captured and never printed. It lives beside its
action because the public mirror copies actions only. → docs/terragrunt-apply-fresh.md

    WORKING_DIRECTORY=terragrunt/dev AWS_REGION=eu-central-1 \\
        python3 .github/actions/clear-state-locks/clear_state_locks.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

AWS_TIMEOUT = 120
GLOBAL_REGION = "us-east-1"
PAGE_SIZE = "1000"
LOCK_QUERY = "{keys: Contents[?ends_with(Key, '.tflock')].Key, next: NextToken}"


class Refused(Exception):
    """An AWS call that failed for a reason other than a missing bucket."""


def warn(reason: str, title: str = "clear-state-locks") -> None:
    print(f"::warning title={title}::{reason}", flush=True)


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


def state_prefix(working_directory: str) -> str | None:
    """`terragrunt/<path from the repository root>/`, the `key` rule of helpers/aws.hcl."""
    parts = [part for part in working_directory.strip().split("/") if part and part != "."]
    if not parts or working_directory.strip().startswith("/") or ".." in parts:
        return None
    return "terragrunt/" + "/".join(parts) + "/"


def aws(args: list[str]) -> subprocess.CompletedProcess | None:
    """Run an AWS CLI call with its output captured; None when the CLI did not run."""
    try:
        return subprocess.run(
            ["aws", *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=AWS_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def account() -> str | None:
    result = aws(["sts", "get-caller-identity", "--query", "Account", "--output", "text"])
    if result is None or result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if value.isdigit() else None


def list_locks(bucket: str, region: str, prefix: str) -> list[str] | None:
    """Every `.tflock` key under the prefix, page by page; None when the bucket does not exist."""
    keys: list[str] = []
    token = ""
    while True:
        args = ["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix, "--region", region,
                "--query", LOCK_QUERY, "--max-items", PAGE_SIZE, "--output", "json"]
        if token:
            args += ["--starting-token", token]
        result = aws(args)
        if result is not None and result.returncode != 0 and "NoSuchBucket" in result.stderr:
            return None
        if result is None or result.returncode != 0:
            raise Refused(f"could not list {bucket}")
        try:
            page = json.loads(result.stdout)
        except ValueError as error:
            raise Refused(f"could not read the listing of {bucket}") from error
        keys += [key for key in page.get("keys") or [] if isinstance(key, str) and key.endswith(".tflock")]
        token = page.get("next") or ""
        if not isinstance(token, str) or not token:
            return keys


def delete(bucket: str, region: str, key: str) -> bool:
    result = aws(["s3api", "delete-object", "--bucket", bucket, "--key", key, "--region", region])
    return result is not None and result.returncode == 0


def sweep(bucket: str, region: str, prefix: str) -> tuple[int, int]:
    """Delete the bucket's locks under the prefix; returns (found, removed)."""
    try:
        keys = list_locks(bucket, region, prefix) or []
    except Refused as error:
        warn(f"Stale state locks were not cleared: {error}")
        return 0, 0
    removed = 0
    for key in keys:
        if delete(bucket, region, key):
            removed += 1
            warn(f"{key} removed. No other run of this environment can hold one.", title="Stale state lock")
        else:
            warn(f"Stale state lock {key} in {bucket} could not be deleted")
    return len(keys), removed


def main() -> int:
    working_directory = os.environ.get("WORKING_DIRECTORY", "")
    region = os.environ.get("AWS_REGION", "").strip()
    try:
        prefix = state_prefix(working_directory)
        if prefix is None or not region:
            warn("Stale state locks were not cleared: no working directory or no AWS region")
            return 0
        owner = account()
        if owner is None:
            warn("Stale state locks were not cleared: could not read the AWS account")
            return 0
        found, removed = sweep(f"{owner}-{region}-terraform-states", region, prefix)
        found_global, removed_global = sweep(f"{owner}-global-terraform-states", GLOBAL_REGION, prefix)
        if found + found_global:
            report(f"{removed + removed_global} stale state locks removed under {prefix}.")
        else:
            report(f"No state lock is held under {prefix}.")
    except Exception:  # noqa: BLE001 - best effort, never fail the run
        warn("Stale state locks were not cleared: unexpected error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
