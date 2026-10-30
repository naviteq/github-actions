#!/usr/bin/env python3
"""Force External Secrets to re-read a secret the run has just written.

Best effort: every path exits 0 and a failure only warns. External Secrets syncs as soon as
cluster-bootstrap installs it, so a stale value left by an earlier environment is read first and
kept until the next refresh; the force-sync annotation makes it read again. It lives beside its
action because the public mirror copies actions only. → docs/terragrunt-apply-fresh.md

    CLOUD=aws CLUSTER_NAME=demo REGION=eu-west-1 EXTERNAL_SECRETS=naviteq-registry-auth-credentials \\
        python3 .github/actions/force-sync-secrets/force_sync_secrets.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
CALL_TIMEOUT = 120
POLL_SECONDS = 10


def warn(reason: str) -> None:
    print(f"::warning title=force-sync-secrets::{reason}", flush=True)


def summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError:
        pass


def run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess | None:
    """Run a command with its output captured; None when it did not run."""
    try:
        return subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=CALL_TIMEOUT,
            check=False, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def succeeded(result: subprocess.CompletedProcess | None) -> bool:
    return result is not None and result.returncode == 0


def write_kubeconfig(path: str) -> str | None:
    """Fetch a kubeconfig into path; None on success, else the reason."""
    cloud = os.environ.get("CLOUD", "")
    region = os.environ.get("REGION", "")
    if cloud == "aws":
        name = os.environ.get("CLUSTER_NAME", "")
        if not name or not region:
            return "no cluster name or region"
        if shutil.which("aws") is None:
            return "no aws CLI"
        done = run(["aws", "eks", "update-kubeconfig", "--name", name, "--region", region, "--kubeconfig", path])
        return None if succeeded(done) else "aws eks update-kubeconfig failed"
    if cloud == "oci":
        unit = os.environ.get("OKE_UNIT_DIR", "")
        if not unit or not region:
            return "no oke unit directory or region"
        if shutil.which("oci") is None or shutil.which("terragrunt") is None:
            return "no oci CLI or terragrunt"
        found = run(["terragrunt", "run", "--working-dir", unit, "--", "output", "-raw", "cluster_id"])
        cluster_id = found.stdout.strip().splitlines()[-1] if succeeded(found) and found.stdout.strip() else ""
        if not cluster_id.startswith("ocid1."):
            return "could not read the cluster id from the oke unit"
        done = run(["oci", "ce", "cluster", "create-kubeconfig", "--cluster-id", cluster_id, "--file", path,
                    "--region", region, "--token-version", "2.0.0",
                    "--kube-endpoint", os.environ.get("KUBE_ENDPOINT") or "PRIVATE_ENDPOINT"])
        return None if succeeded(done) else "oci ce cluster create-kubeconfig failed"
    return f"unknown cloud {cloud!r}"


def wait_for(kubectl: list[str], env: dict[str, str], namespace: str, name: str, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if succeeded(run([*kubectl, "get", "externalsecret", name, "-n", namespace, "-o", "name"], env)):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def sync(kubectl: list[str], env: dict[str, str], namespace: str, name: str, seconds: int) -> None:
    if not wait_for(kubectl, env, namespace, name, seconds):
        warn(f"ExternalSecret {namespace}/{name} was not found, so it was not synced; it picks the value up at its next refresh")
        return
    stamp = str(int(time.time()))
    done = run([*kubectl, "annotate", "externalsecret", name, "-n", namespace, f"force-sync={stamp}", "--overwrite"], env)
    if succeeded(done):
        line = f"ExternalSecret {namespace}/{name} force-synced"
        print(line, flush=True)
        summary(line)
    else:
        warn(f"ExternalSecret {namespace}/{name} could not be annotated; it picks the value up at its next refresh")


def main() -> int:
    names = os.environ.get("EXTERNAL_SECRETS", "").split()
    namespace = os.environ.get("NAMESPACE", "") or "argo"
    try:
        seconds = max(0, int(os.environ.get("WAIT_SECONDS", "") or 180))
    except ValueError:
        seconds = 180
    try:
        if not names:
            print("Force-sync skipped: no ExternalSecret named", flush=True)
            return 0
        if not NAME_RE.match(namespace) or not all(NAME_RE.match(name) for name in names):
            warn("Force-sync skipped: a namespace or ExternalSecret name is not a valid Kubernetes name")
            return 0
        if shutil.which("kubectl") is None:
            warn("Force-sync skipped: no kubectl on this runner")
            return 0
        with tempfile.TemporaryDirectory(prefix="force-sync-") as directory:
            path = os.path.join(directory, "kubeconfig")
            reason = write_kubeconfig(path)
            if reason:
                warn(f"Force-sync skipped: {reason}")
                return 0
            if os.path.exists(path):
                os.chmod(path, 0o600)
            env = {**os.environ, "KUBECONFIG": path}
            for name in names:
                sync(["kubectl"], env, namespace, name, seconds)
    except Exception as error:  # noqa: BLE001 - best effort, never fail the apply
        warn(f"Force-sync did not finish: unexpected {type(error).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
