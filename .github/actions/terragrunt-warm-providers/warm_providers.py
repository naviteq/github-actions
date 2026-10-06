#!/usr/bin/env python3
"""Fill a provider cache with every provider version the units' lock files pin.

Reads each committed .terraform.lock.hcl, so it needs no state, credentials or
dependency outputs, and runs a plain `init -backend=false` with TF_PLUGIN_CACHE_DIR,
whose layout Terragrunt's provider cache reuses. It lives beside its action, not in
scripts/, because the public mirror copies actions only. → docs/terragrunt-plan.md

    python3 .github/actions/terragrunt-warm-providers/warm_providers.py \\
        --working-directory terragrunt --engine tofu --cache-dir "$TG_PROVIDER_CACHE_DIR"
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROVIDER = re.compile(r'provider\s+"(?P<source>[^"]+)"\s*\{\s*version\s*=\s*"(?P<version>[^"]+)"')
SKIP_DIRS = {".terragrunt-cache", ".terraform", ".git"}


def find_locks(root: Path) -> list[Path]:
    """Committed lock files under root, outside Terragrunt's and the engine's caches."""
    locks = []
    for path in sorted(root.rglob(".terraform.lock.hcl")):
        if not SKIP_DIRS.intersection(path.relative_to(root).parts[:-1]):
            locks.append(path)
    return locks


def pinned_versions(texts: list[str]) -> dict[str, list[str]]:
    """Every distinct version of each provider source, sorted."""
    found: dict[str, set[str]] = {}
    for text in texts:
        for match in PROVIDER.finditer(text):
            found.setdefault(match["source"], set()).add(match["version"])
    return {source: sorted(versions) for source, versions in sorted(found.items())}


def batches(versions: dict[str, list[str]]) -> list[dict[str, str]]:
    """Split into configurations that each require one version per provider."""
    depth = max((len(v) for v in versions.values()), default=0)
    return [{s: v[i] for s, v in versions.items() if i < len(v)} for i in range(depth)]


def render(batch: dict[str, str]) -> str:
    lines = ["terraform {", "  required_providers {"]
    for index, (source, version) in enumerate(batch.items()):
        lines.append(f'    p{index} = {{ source = "{source}", version = "{version}" }}')
    lines += ["  }", "}", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--working-directory", default=".")
    parser.add_argument("--engine", default="tofu")
    parser.add_argument("--cache-dir", required=True)
    args = parser.parse_args(argv)

    locks = find_locks(Path(args.working_directory))
    versions = pinned_versions([lock.read_text(encoding="utf-8") for lock in locks])
    total = sum(len(v) for v in versions.values())
    print(f"{total} provider versions pinned by {len(locks)} lock files")
    if not total:
        print("::notice title=Nothing to warm::No lock file pins a provider; commit the units' .terraform.lock.hcl.")
        return 0

    Path(args.cache_dir).mkdir(parents=True, exist_ok=True)
    # The engine fills the cache itself; Terragrunt's cache server must stay out of it.
    env = {k: v for k, v in os.environ.items() if not k.startswith("TG_PROVIDER_CACHE")}
    env["TF_PLUGIN_CACHE_DIR"] = str(Path(args.cache_dir).resolve())
    for batch in batches(versions):
        with tempfile.TemporaryDirectory() as workdir:
            Path(workdir, "versions.tf").write_text(render(batch), encoding="utf-8")
            result = subprocess.run([args.engine, "init", "-backend=false", "-input=false", "-no-color"],
                                    cwd=workdir, env=env, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                print(result.stdout + result.stderr)
                print(f"::error title=Provider cache warm-up failed::{args.engine} init could not fetch "
                      + ", ".join(f"{s} {v}" for s, v in batch.items()))
                return 1
    print(f"Warmed {args.cache_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
