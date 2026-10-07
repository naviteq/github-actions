#!/usr/bin/env python3
"""Select the Terragrunt units a run should cover, and show them in run order.

Shared by terragrunt-validate-lint and terragrunt-plan. It lives beside its action, not
in scripts/, because the public mirror copies actions only. → docs/terragrunt-plan.md

    python3 .github/actions/terragrunt-select-units/select_units.py --working-directory terragrunt \\
        --filter-affected --base main --with-dependents --verb plan
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

FENCE = "```"
# A git range runs units in a temporary checkout of each revision, and `list` may print a
# unit by its path there: ../../tmp/terragrunt-worktree-HEAD-123/<repository path>.
WORKTREE = re.compile(r"(?:^|/)terragrunt-worktree-[^/]*-\d+/(?P<path>.+)$")


def resolve_filter(
    explicit: str, affected: bool, with_dependents: bool, base: str, scope: str
) -> str:
    """The --filter expression for this run; empty selects every unit."""
    if not affected:
        return explicit
    expression = f"[origin/{base}...HEAD]"
    # A leading ... pulls in every unit that depends on a selected one.
    if with_dependents:
        expression = f"...{expression}"
    scope = scope.strip()
    if scope.startswith("./"):
        scope = scope[2:]
    scope = scope.rstrip("/")
    # A git range is discovered from the repository root, not working_directory.
    if scope and scope != ".":
        expression = f"./{scope}/** | {expression}"
    return expression


def parse_units(table: str) -> list[str]:
    """Unit paths from `terragrunt list --long`, in the order it printed them."""
    units = []
    for line in table.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "unit":
            units.append(fields[1])
    return units


def from_worktree(unit: str, prefix: str) -> str:
    """A unit path inside a range's temporary checkout, as a path under the working directory."""
    match = WORKTREE.search(unit)
    if not match:
        return unit
    path = match["path"]
    return path[len(prefix):] if prefix and path.startswith(prefix) else path


def split_existing(units: list[str], root: Path, prefix: str = "") -> tuple[list[str], list[str]]:
    """Units present in this revision, and the ones a git range found deleted."""
    seen = list(dict.fromkeys(from_worktree(unit, prefix) for unit in units))
    existing = [unit for unit in seen if (root / unit).is_dir()]
    deleted = [unit for unit in seen if not (root / unit).is_dir()]
    return existing, deleted


def repository_prefix(root: Path) -> str:
    """The working directory's path from the repository root, with a trailing slash."""
    shown = subprocess.run(["git", "rev-parse", "--show-prefix"], cwd=root, capture_output=True, text=True,
                           check=False)
    return shown.stdout.strip() if shown.returncode == 0 else ""


def summary(
    verb: str, participle: str, expression: str, table: str, existing: list[str], deleted: list[str]
) -> str:
    lines = [f"### Units to {verb}, in run order", "", f"Filter: `{expression or 'none'}`", ""]
    if existing:
        lines += [f"{FENCE}text", table, FENCE]
    else:
        lines.append("The filter selects no units.")
    if deleted:
        lines += ["", f"Deleted by this change, so not {participle} here:"]
        lines += [f"- `{unit}`" for unit in deleted]
    return "\n".join(lines) + "\n"


def _write_outputs(path: str, values: dict[str, str]) -> None:
    with open(path, "a", encoding="utf-8") as out:
        for key, value in values.items():
            out.write(f"{key}<<SELECT_UNITS_EOF\n{value}\nSELECT_UNITS_EOF\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--working-directory", default=".")
    # Relative to the repository root; defaults to --working-directory.
    parser.add_argument("--scope", default=None)
    parser.add_argument("--filter", default="")
    parser.add_argument("--filter-affected", action="store_true")
    parser.add_argument("--with-dependents", action="store_true")
    parser.add_argument("--base", default="main")
    parser.add_argument("--verb", default="validate")
    parser.add_argument("--participle", default="validated")
    # A unit whose exclude block names this command is left out, as the run would skip it.
    parser.add_argument("--queue-as", default="")
    parser.add_argument("--terragrunt", default=os.environ.get("TERRAGRUNT_BIN", "terragrunt"))
    args = parser.parse_args(argv)

    expression = resolve_filter(
        args.filter,
        args.filter_affected,
        args.with_dependents,
        args.base,
        args.working_directory if args.scope is None else args.scope,
    )
    command = [args.terragrunt, "list", "--long", "--dependencies", "--dag", "--non-interactive"]
    if expression:
        command += ["--filter", expression]
    if args.queue_as:
        command += ["--queue-construct-as", args.queue_as]
    root = Path(args.working_directory)
    listed = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        sys.stderr.write(listed.stderr)
        print(f"::error title=Unit selection failed::terragrunt list exited {listed.returncode}")
        return listed.returncode

    table = listed.stdout.rstrip("\n")
    existing, deleted = split_existing(parse_units(table), root, repository_prefix(root))

    print(table if existing else "The filter selects no units.")
    if deleted:
        print(f"Deleted by this change, not {args.participle} here: {' '.join(deleted)}")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
            out.write(summary(args.verb, args.participle, expression, table, existing, deleted))
    if os.environ.get("GITHUB_OUTPUT"):
        _write_outputs(
            os.environ["GITHUB_OUTPUT"],
            {"filter": expression, "units": "\n".join(existing), "deleted": "\n".join(deleted)},
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
