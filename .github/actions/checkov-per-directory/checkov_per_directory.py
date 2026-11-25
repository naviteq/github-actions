#!/usr/bin/env python3
"""Run Checkov once per top-level subdirectory of a scan root, then merge the reports.

    checkov_per_directory.py --directory DIR --report checkov-report.txt \
        --sarif results.sarif -- <checkov arguments>

Every pass keeps DIR as the scan root and skips the other subdirectories with
--skip-path, so finding paths and baseline entries stay relative to DIR, exactly as
in a single `checkov -d DIR`. A last pass skips every subdirectory and covers the files
directly in DIR. Peak memory is the heaviest pass, not the whole tree.

The CLI report gets one section per pass with failed checks or a non-zero exit; the SARIF
reports are merged into one run. Exit code is the highest exit code of any pass.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

Runner = Callable[[Sequence[str]], int]

# Checkov prints its summary even with --quiet, so an empty pass still has text.
FOUND = re.compile(r"\bFailed checks: [1-9]")


def list_scopes(root: str) -> list[str]:
    """Top-level subdirectories of root, hidden ones excluded, in a stable order."""
    return sorted(
        entry.name
        for entry in os.scandir(root)
        if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".")
    )


def skip_patterns(root: str, scopes: Sequence[str], keep: str | None) -> list[str]:
    """--skip-path regexes for every scope but keep, anchored to the scan root.

    Checkov matches each pattern against the walked path, which is absolute for the
    terraform framework and as-given for the others, so both spellings are anchored.
    Anchoring keeps a nested directory that shares a scope's name inside the scan.
    """
    rel = os.path.normpath(root)
    absolute = os.path.abspath(root)
    roots = "|".join(re.escape(r) for r in dict.fromkeys((absolute, rel)))
    return [f"^({roots})/{re.escape(scope)}$" for scope in scopes if scope != keep]


def merge_sarif(documents: Sequence[dict]) -> dict:
    """One SARIF document, one run: the union of rules by id and every result.

    ruleIndex is dropped because it points into a per-pass rules array; ruleId stays.
    """
    runs = [run for document in documents for run in document.get("runs", [])]
    if not runs:
        return {"version": "2.1.0", "runs": []}
    rules: dict[str, dict] = {}
    results: list[dict] = []
    for run in runs:
        for rule in run.get("tool", {}).get("driver", {}).get("rules", []):
            rules.setdefault(rule["id"], rule)
        for result in run.get("results", []):
            results.append({k: v for k, v in result.items() if k != "ruleIndex"})
    first = runs[0]
    driver = {**first.get("tool", {}).get("driver", {}), "rules": list(rules.values())}
    merged_run = {**first, "tool": {**first.get("tool", {}), "driver": driver}, "results": results}
    return {**documents[0], "runs": [merged_run]}


def run_checkov(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False).returncode


def scan(
    root: str,
    checkov_args: Sequence[str],
    report: Path,
    sarif: Path,
    parts: Path,
    run: Runner = run_checkov,
) -> int:
    scopes = list_scopes(root)
    print("::group::Top-level directories scanned one at a time")
    for scope in scopes or ["(none)"]:
        print(f"  {scope}")
    print("::endgroup::")

    rc = 0
    sections: list[str] = []
    sarif_parts: list[Path] = []
    passes: list[str | None] = [*scopes, None]
    for n, keep in enumerate(passes, start=1):
        label = keep if keep is not None else f"(files in {root})"
        part = parts / f"{n:04d}"
        skips = [arg for pattern in skip_patterns(root, scopes, keep) for arg in ("--skip-path", pattern)]
        argv = ["checkov", "-d", root, *checkov_args, *skips,
                "--output-file-path", f"{part}.txt,{part}.sarif"]
        print(f"::group::Checkov: {label}", flush=True)
        part_rc = run(argv)
        print("::endgroup::", flush=True)
        print(f"{label}: exit {part_rc}", flush=True)
        rc = max(rc, part_rc)

        text_path = Path(f"{part}.txt")
        text = text_path.read_text(encoding="utf-8") if text_path.is_file() else ""
        if part_rc != 0 or FOUND.search(text):
            sections.append(f"### {label} (exit {part_rc})\n{text}\n")
        if Path(f"{part}.sarif").is_file():
            sarif_parts.append(Path(f"{part}.sarif"))

    report.write_text("".join(sections), encoding="utf-8")
    if sarif_parts:
        documents = [json.loads(p.read_text(encoding="utf-8")) for p in sarif_parts]
        sarif.write_text(json.dumps(merge_sarif(documents), indent=2), encoding="utf-8")
    return rc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--directory", default=".")
    parser.add_argument("--report", default="checkov-report.txt")
    parser.add_argument("--sarif", default="results.sarif")
    parser.add_argument("checkov_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    checkov_args = args.checkov_args[1:] if args.checkov_args[:1] == ["--"] else args.checkov_args
    root = os.path.normpath(args.directory.rstrip("/") or ".")
    with tempfile.TemporaryDirectory(prefix="checkov-parts-", dir=os.environ.get("RUNNER_TEMP")) as parts:
        return scan(root, checkov_args, Path(args.report), Path(args.sarif), Path(parts))


if __name__ == "__main__":
    sys.exit(main())
