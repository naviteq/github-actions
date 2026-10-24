#!/usr/bin/env python3
"""Delete every image in the ECR repositories the generated ecr units manage, ahead of a destroy.

Best effort: every path exits 0 and a refused call only warns. The ecr unit keeps
`force_delete = false`, so its destroy fails on a repository that still holds images; emptying the
repositories first lets the unchanged destroy delete them. Repository names come only from each
unit's own `wrapper` output, read with a single-unit terragrunt run; nothing else names one.
Terragrunt and CLI output is captured and never printed. It lives beside its action because the
public mirror copies actions only. → docs/terragrunt-apply-fresh.md

    WORKING_DIRECTORY=terragrunt/dev AWS_REGION=eu-central-1 \\
        python3 .github/actions/empty-ecr-repositories/empty_ecr_repositories.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

AWS_TIMEOUT = 120
TERRAGRUNT_TIMEOUT = 600
PUBLIC_REGION = "us-east-1"
PUBLIC_HOST = "public.ecr.aws"
BATCH_SIZE = 100
PAGE_SIZE = "1000"
DELETE_PASSES = 3
TITLE = "ECR purge"
STACK_DIR = ".terragrunt-stack"
SKIPPED_DIRS = {".terragrunt-cache", ".terraform", ".git"}
ECR_SOURCE_RE = re.compile(r'^\s*source\s*=\s*"[^"]*/naviteq/ecr/aws//wrappers', re.M)
# A child manifest cannot go while its image index stands; it is retried once the index is gone.
RETRYABLE = {"ImageReferencedByManifestList"}


class Refused(Exception):
    """An AWS call that failed for a reason other than a missing repository."""


def warn(reason: str) -> None:
    print(f"::warning title={TITLE}::{reason}", flush=True)


def notice(reason: str) -> None:
    print(f"::notice title={TITLE}::{reason}", flush=True)


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


def run(command: list[str], timeout: int) -> subprocess.CompletedProcess | None:
    """Run a command with its output captured; None when it did not run or did not finish."""
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def find_ecr_units(working_directory: str) -> list[Path]:
    """Generated units under a `.terragrunt-stack` directory whose terragrunt.hcl sources the ecr wrapper."""
    units = []
    for directory, subdirectories, files in os.walk(working_directory):
        subdirectories[:] = sorted(name for name in subdirectories if name not in SKIPPED_DIRS)
        path = Path(directory)
        if "terragrunt.hcl" not in files or STACK_DIR not in path.parts:
            continue
        try:
            text = (path / "terragrunt.hcl").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if ECR_SOURCE_RE.search(text):
            units.append(path)
    return sorted(units)


def unit_repositories(unit: Path) -> list[tuple[str, bool]] | None:
    """(repository name, public) per entry of the unit's `wrapper` output; None when it cannot be read."""
    result = run(["terragrunt", "run", "--non-interactive", "--working-dir", str(unit), "--", "output", "-json"],
                 TERRAGRUNT_TIMEOUT)
    if result is None or result.returncode != 0:
        return None
    try:
        outputs = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(outputs, dict):
        return None
    value = (outputs.get("wrapper") or {}).get("value") if isinstance(outputs.get("wrapper"), dict) else None
    if not isinstance(value, dict):
        return []
    repositories = []
    for entry in value.values():
        if not isinstance(entry, dict):
            continue
        name = entry.get("repository_name")
        url = entry.get("repository_url")
        if isinstance(name, str) and name:
            repositories.append((name, isinstance(url, str) and url.startswith(PUBLIC_HOST)))
    return repositories


def aws_json(args: list[str]) -> dict | None:
    """Parsed JSON of an AWS CLI call; None for a missing repository, Refused for any other failure."""
    result = run(["aws", *args, "--output", "json"], AWS_TIMEOUT)
    if result is not None and result.returncode != 0 and "RepositoryNotFoundException" in result.stderr:
        return None
    if result is None or result.returncode != 0:
        raise Refused(f"{args[0]} {args[1]} failed")
    try:
        parsed = json.loads(result.stdout or "{}")
    except ValueError as error:
        raise Refused(f"{args[0]} {args[1]} answered something other than JSON") from error
    return parsed if isinstance(parsed, dict) else {}


def digests(repository: str, region: str, public: bool) -> list[str] | None:
    """Every image digest in the repository, page by page; None when it does not exist."""
    service, operation, key = ("ecr-public", "describe-images", "imageDetails") if public else (
        "ecr", "list-images", "imageIds")
    found: set[str] = set()
    token = ""
    while True:
        args = [service, operation, "--repository-name", repository, "--region", region, "--max-items", PAGE_SIZE]
        if token:
            args += ["--starting-token", token]
        page = aws_json(args)
        if page is None:
            return None
        found |= {item["imageDigest"] for item in page.get(key) or []
                  if isinstance(item, dict) and isinstance(item.get("imageDigest"), str)}
        token = page.get("NextToken") or ""
        if not isinstance(token, str) or not token:
            return sorted(found)


def delete(repository: str, region: str, public: bool, chunk: list[str]) -> dict[str, str]:
    """Delete one batch; returns the failure code per digest that is still there."""
    service = "ecr-public" if public else "ecr"
    image_ids = json.dumps([{"imageDigest": item} for item in chunk], separators=(",", ":"))
    answer = aws_json([service, "batch-delete-image", "--repository-name", repository, "--region", region,
                       "--image-ids", image_ids]) or {}
    failed = {}
    for failure in answer.get("failures") or []:
        if not isinstance(failure, dict):
            continue
        item = (failure.get("imageId") or {}).get("imageDigest") if isinstance(failure.get("imageId"), dict) else None
        if isinstance(item, str) and item in chunk:
            failed[item] = str(failure.get("failureCode") or "unknown")
    return failed


def empty(repository: str, region: str, public: bool) -> tuple[int, dict[str, str]] | None:
    """Delete every image in batches of 100: (deleted, failure code per digest kept), None when absent."""
    region = PUBLIC_REGION if public else region
    remaining = digests(repository, region, public)
    if remaining is None:
        return None
    total = len(remaining)
    kept: dict[str, str] = {}
    for attempt in range(DELETE_PASSES):
        failed: dict[str, str] = {}
        for start in range(0, len(remaining), BATCH_SIZE):
            failed.update(delete(repository, region, public, remaining[start:start + BATCH_SIZE]))
        retry = sorted(item for item, code in failed.items() if code in RETRYABLE)
        kept.update({item: code for item, code in failed.items() if code not in RETRYABLE})
        if retry and (len(retry) == len(remaining) or attempt == DELETE_PASSES - 1):
            kept.update({item: failed[item] for item in retry})
            break
        if not retry:
            break
        remaining = retry
    return total - len(kept), kept


def purge(repository: str, region: str, public: bool) -> None:
    try:
        outcome = empty(repository, region, public)
    except Refused as error:
        warn(f"{repository} was not emptied: {error}")
        return
    if outcome is None:
        report(f"{repository}: no such repository")
        return
    deleted, failed = outcome
    if failed:
        codes = ", ".join(sorted(set(failed.values())))
        warn(f"{repository} still holds {len(failed)} images ({codes})")
    report(f"{repository}: {deleted} images deleted")


def main() -> int:
    working_directory = os.environ.get("WORKING_DIRECTORY", "").strip()
    region = os.environ.get("AWS_REGION", "").strip()
    try:
        if not working_directory or not region:
            warn("ECR repositories were not emptied: no working directory or no AWS region")
            return 0
        units = find_ecr_units(working_directory)
        if not units:
            report(f"No ECR unit in {working_directory}.")
            return 0
        for unit in units:
            repositories = unit_repositories(unit)
            if repositories is None:
                warn(f"ECR repositories of {unit} were not emptied: its outputs could not be read")
                continue
            if not repositories:
                notice(f"{unit} has no repository in its outputs; nothing to empty")
                continue
            for repository, public in repositories:
                purge(repository, region, public)
    except Exception:  # noqa: BLE001 - best effort, never fail the destroy
        warn("ECR repositories were not emptied: unexpected error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
