#!/usr/bin/env python3
"""Move the module sources Terragrunt downloaded in and out of a cacheable store.

pack scrubs the .terragrunt-cache that `hcl validate --inputs --all` leaves in the working
directory and moves it into the store; unpack puts the store back, either in that same
place (layout root) or split across the units, where `run --all` downloads (layout unit).
It lives beside its action because the public mirror copies actions only.
→ docs/terragrunt-validate-lint.md

    python3 .github/actions/terragrunt-module-cache/module_cache.py pack \\
        --working-directory . --store "$RUNNER_TEMP/terragrunt-modules"
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import shutil
import sys
from pathlib import Path

CACHE_DIR = ".terragrunt-cache"
VERSION_FILE = ".terragrunt-source-version"
MANIFEST = ".terragrunt-module-manifest"
UNIT_FILE = "terragrunt.hcl"


def unit_hash(unit: Path) -> str:
    """The directory name Terragrunt gives a unit's downloads: base64url(sha1(its path))."""
    digest = hashlib.sha1(os.path.normpath(str(unit)).encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def absolute(path: Path) -> Path:
    """Absolute without resolving links, the way Terragrunt names the path from $PWD."""
    if path.is_absolute():
        return Path(os.path.normpath(path))
    return Path(os.path.normpath(Path(os.environ.get("PWD") or os.getcwd()) / path))


def units(root: Path) -> list[Path]:
    """Every unit directory under root, generated stack units included."""
    return sorted(p.parent for p in root.rglob(UNIT_FILE) if CACHE_DIR not in p.relative_to(root).parts)


def written_after(version: Path) -> list[Path]:
    """Files and links below the source with a later modification time than its version file."""
    stamp = version.stat().st_mtime_ns
    return sorted(
        path for path in version.parent.rglob("*")
        if (path.is_symlink() or path.is_file()) and path.lstat().st_mtime_ns > stamp
    )


def scrub(cache: Path) -> tuple[int, int]:
    """Keep each source this run used, without what the run wrote over it; return (kept, dropped)."""
    kept = dropped = 0
    for unit_dir in sorted(p for p in cache.iterdir() if p.is_dir()) if cache.is_dir() else []:
        for source in sorted(p for p in unit_dir.iterdir() if p.is_dir()):
            version = source / VERSION_FILE
            # Terragrunt rewrites the manifest whenever it copies a unit over its source, in the
            # source's subdirectory when the URL names one (//modules/x).
            if not version.is_file() or not any(p.is_file() for p in source.rglob(MANIFEST)):
                shutil.rmtree(source)
                dropped += 1
                continue
            for path in written_after(version):
                path.unlink()
            kept += 1
        if not any(unit_dir.iterdir()):
            unit_dir.rmdir()
    return kept, dropped


def pack(working_directory: Path, store: Path) -> int:
    cache = working_directory / CACHE_DIR
    kept, dropped = scrub(cache)
    shutil.rmtree(store, ignore_errors=True)
    store.parent.mkdir(parents=True, exist_ok=True)
    if cache.is_dir():
        shutil.move(str(cache), str(store))
    else:
        store.mkdir()
    print(f"Packed {kept} module sources into {store}; dropped {dropped} this run did not use")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"sources={kept}\n")
    return 0


def unpack(working_directory: Path, store: Path, layout: str) -> int:
    if not store.is_dir():
        print("Nothing to unpack: the cache was not restored")
        return 0
    moved = 0
    if layout == "root":
        cache = working_directory / CACHE_DIR
        cache.mkdir(exist_ok=True)
        for entry in sorted(store.iterdir()):
            if not (cache / entry.name).exists():
                shutil.move(str(entry), str(cache / entry.name))
                moved += 1
    else:
        for unit in units(working_directory):
            entry = store / unit_hash(absolute(unit))
            target = unit / CACHE_DIR / entry.name
            if entry.is_dir() and not target.exists():
                target.parent.mkdir(exist_ok=True)
                shutil.move(str(entry), str(target))
                moved += 1
    print(f"Unpacked the module sources of {moved} units")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["pack", "unpack"])
    parser.add_argument("--working-directory", default=".")
    parser.add_argument("--store", required=True)
    parser.add_argument("--layout", choices=["root", "unit"], default="root")
    args = parser.parse_args(argv)

    working_directory, store = Path(args.working_directory), Path(args.store)
    if args.command == "pack":
        return pack(working_directory, store)
    return unpack(working_directory, store, args.layout)


if __name__ == "__main__":
    sys.exit(main())
