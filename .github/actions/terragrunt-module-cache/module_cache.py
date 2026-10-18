#!/usr/bin/env python3
"""Move the module sources Terragrunt downloaded in and out of a cacheable store.

pack scrubs the .terragrunt-cache that `hcl validate --inputs --all` leaves in the working
directory and moves it into the store; unpack puts the store back, either in that same
place (layout root) or split across the units, where `run --all` downloads (layout unit).
The store carries digests of the configuration it was saved from, and unpack restores only
units whose configuration still matches. digest prints the fingerprint the cache key uses.
It lives beside its action because the public mirror copies actions only.
→ docs/terragrunt-validate-lint.md

    python3 .github/actions/terragrunt-module-cache/module_cache.py pack \\
        --working-directory . --store "$RUNNER_TEMP/terragrunt-modules"
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

CACHE_DIR = ".terragrunt-cache"
VERSION_FILE = ".terragrunt-source-version"
MANIFEST = ".terragrunt-module-manifest"
UNIT_FILE = "terragrunt.hcl"
STACK_DIR = ".terragrunt-stack"
DIGESTS = "module-cache-digests.json"
CONFIG_SUFFIXES = (".hcl", ".yaml", ".yml", ".json")


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


def config_files(directory: Path) -> list[Path]:
    """The files directly in directory that Terragrunt configuration is written in or reads."""
    return sorted(p for p in directory.iterdir() if p.name.endswith(CONFIG_SUFFIXES) and p.is_file())


def digest(files: list[Path], base: Path) -> str:
    """sha256 over each file's path relative to base and its content."""
    hashed = hashlib.sha256()
    for path in sorted(files):
        content = path.read_bytes()
        hashed.update(f"{os.path.relpath(path, base)}\0{len(content)}\0".encode())
        hashed.update(content)
    return hashed.hexdigest()


def fingerprint(repository: Path, working_directory: Path) -> dict:
    """A shared digest over the configuration outside every unit, and one per unit below working_directory."""
    shared = []
    # Hidden directories other than generated stacks are skipped, .terragrunt-cache among them.
    for directory, subdirectories, names in os.walk(repository):
        subdirectories[:] = [d for d in subdirectories if d == STACK_DIR or not d.startswith(".")]
        if UNIT_FILE not in names:
            shared.extend(config_files(Path(directory)))
    return {
        "shared": digest(shared, repository),
        "units": {unit_hash(absolute(unit)): digest(config_files(unit), unit) for unit in units(working_directory)},
    }


def write_output(name: str, value: str) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


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


def make_private(source: Path) -> None:
    """Give every file below source its own writable inode, so a later download can overwrite it."""
    # hcl validate ignores TG_NO_CAS and leaves read-only hardlinks. → see docs/terragrunt-validate-lint.md
    for directory in [source, *(p for p in source.rglob("*") if p.is_dir() and not p.is_symlink())]:
        directory.chmod(stat.S_IMODE(directory.stat().st_mode) | stat.S_IRWXU)
    for path in source.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        info = path.stat()
        if info.st_nlink == 1 and info.st_mode & stat.S_IWUSR:
            continue
        copy = path.with_name(f"{path.name}.module-cache")
        shutil.copyfile(path, copy)
        copy.chmod(stat.S_IMODE(info.st_mode) | stat.S_IWUSR)
        os.utime(copy, ns=(info.st_atime_ns, info.st_mtime_ns))
        os.replace(copy, path)


def pack(working_directory: Path, store: Path, repository: Path) -> int:
    cache = working_directory / CACHE_DIR
    kept, dropped = scrub(cache)
    shutil.rmtree(store, ignore_errors=True)
    store.parent.mkdir(parents=True, exist_ok=True)
    if cache.is_dir():
        shutil.move(str(cache), str(store))
    else:
        store.mkdir()
    current = fingerprint(repository, working_directory)
    current["units"] = {name: value for name, value in current["units"].items() if (store / name).is_dir()}
    (store / DIGESTS).write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Packed {kept} module sources into {store}; dropped {dropped} this run did not use")
    write_output("sources", str(kept))
    return 0


def unpack(working_directory: Path, store: Path, layout: str, repository: Path) -> int:
    if not store.is_dir():
        print("Nothing to unpack: the cache was not restored")
        return 0
    if not (store / DIGESTS).is_file():
        print("Nothing to unpack: the cache was saved without configuration digests")
        return 0
    saved = json.loads((store / DIGESTS).read_text(encoding="utf-8"))
    current = fingerprint(repository, working_directory)
    if saved.get("shared") != current["shared"]:
        print("Nothing to unpack: configuration outside the units changed since the cache was saved")
        return 0
    moved = skipped = 0
    for unit in units(working_directory):
        entry = store / unit_hash(absolute(unit))
        if not entry.is_dir():
            continue
        # A changed unit may name a new source version, which Terragrunt unpacks over the old one.
        if saved.get("units", {}).get(entry.name) != current["units"][entry.name]:
            skipped += 1
            continue
        target = (working_directory if layout == "root" else unit) / CACHE_DIR / entry.name
        if not target.exists():
            target.parent.mkdir(exist_ok=True)
            shutil.move(str(entry), str(target))
            make_private(target)
            moved += 1
    print(f"Unpacked the module sources of {moved} units; skipped {skipped} whose configuration changed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["pack", "unpack", "digest"])
    parser.add_argument("--working-directory", default=".")
    parser.add_argument("--repository", default=".", help="Repository root, the scope of the shared digest")
    parser.add_argument("--store")
    parser.add_argument("--layout", choices=["root", "unit"], default="root")
    args = parser.parse_args(argv)

    working_directory, repository = Path(args.working_directory), Path(args.repository)
    if args.command == "digest":
        current = json.dumps(fingerprint(repository, working_directory), sort_keys=True)
        value = hashlib.sha256(current.encode()).hexdigest()
        print(f"Configuration digest {value}")
        write_output("digest", value)
        return 0
    if not args.store:
        parser.error(f"{args.command} needs --store")
    if args.command == "pack":
        return pack(working_directory, Path(args.store), repository)
    return unpack(working_directory, Path(args.store), args.layout, repository)


if __name__ == "__main__":
    sys.exit(main())
