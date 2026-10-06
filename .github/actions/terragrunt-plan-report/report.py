#!/usr/bin/env python3
"""Count what each Terragrunt plan would do, without printing any planned value.

Reads the tfplan every planned unit left in its .terragrunt-cache, and the destroy plans
of deleted units from the run's log. → docs/terragrunt-plan.md

    python3 .github/actions/terragrunt-plan-report/report.py --working-directory terragrunt \\
        --engine tofu --units "$UNITS" --deleted "$DELETED" --destroy-log destroy.log
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PLAN_LINE = re.compile(
    r"\[(?P<unit>[^\]]+)\].*Plan: (?P<add>\d+) to add, (?P<change>\d+) to change, (?P<destroy>\d+) to destroy"
)
NO_CHANGES = re.compile(r"\[(?P<unit>[^\]]+)\].*No changes\.")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def count_actions(plan: dict) -> dict[str, int]:
    """Tally resource_changes by action; a replace is counted once, as a replace."""
    counts = {"add": 0, "change": 0, "replace": 0, "destroy": 0}
    for change in plan.get("resource_changes") or []:
        actions = change.get("change", {}).get("actions", [])
        if "create" in actions and "delete" in actions:
            counts["replace"] += 1
        elif actions == ["create"]:
            counts["add"] += 1
        elif actions == ["update"]:
            counts["change"] += 1
        elif actions == ["delete"]:
            counts["destroy"] += 1
    return counts


def find_plan(unit_dir: Path) -> Path | None:
    """The newest tfplan Terragrunt left under the unit's cache."""
    plans = sorted(unit_dir.glob(".terragrunt-cache/**/tfplan"), key=lambda p: p.stat().st_mtime)
    return plans[-1] if plans else None


def destroy_counts(log: str, deleted: list[str]) -> dict[str, dict[str, int]]:
    """Destroy-plan counts per deleted unit, read from `[unit] ... Plan:` log lines."""
    found: dict[str, dict[str, int]] = {}
    for line in ANSI.sub("", log).splitlines():
        match = PLAN_LINE.search(line)
        if match:
            counts = {"add": int(match["add"]), "change": int(match["change"]), "replace": 0,
                      "destroy": int(match["destroy"])}
        elif NO_CHANGES.search(line):
            match = NO_CHANGES.search(line)
            counts = {"add": 0, "change": 0, "replace": 0, "destroy": 0}
        else:
            continue
        for unit in deleted:
            if match["unit"] == unit or match["unit"].endswith("/" + unit):
                found[unit] = counts
    return found


def table(rows: list[dict]) -> str:
    lines = ["| Unit | Add | Change | Replace | Destroy |", "|---|---|---|---|---|"]
    for row in rows:
        unit = f"`{row['unit']}`" + (" (deleted)" if row.get("deleted") else "")
        if row.get("error"):
            lines.append(f"| {unit} | {row['error']} | | | |")
            continue
        lines.append(f"| {unit} | {row['add']} | {row['change']} | {row['replace']} | {row['destroy']} |")
    return "\n".join(lines) + "\n"


def _lines(value: str) -> list[str]:
    return [line for line in value.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--working-directory", default=".")
    parser.add_argument("--engine", default="tofu")
    parser.add_argument("--units", default="")
    parser.add_argument("--deleted", default="")
    parser.add_argument("--destroy-log", default="")
    args = parser.parse_args(argv)

    root = Path(args.working_directory)
    rows: list[dict] = []
    plans: list[str] = []
    for unit in _lines(args.units):
        plan_file = find_plan(root / unit)
        if plan_file is None:
            rows.append({"unit": unit, "error": "no plan file"})
            continue
        shown = subprocess.run(
            [args.engine, "show", "-json", plan_file.name],
            cwd=plan_file.parent, capture_output=True, text=True, check=False,
        )
        if shown.returncode != 0:
            rows.append({"unit": unit, "error": "plan unreadable"})
            continue
        rows.append({"unit": unit, **count_actions(json.loads(shown.stdout))})
        plans.append(f"{unit}\t{plan_file}")

    deleted = _lines(args.deleted)
    log_file = Path(args.destroy_log) if args.destroy_log else None
    log = log_file.read_text(encoding="utf-8") if log_file and log_file.is_file() else ""
    found = destroy_counts(log, deleted)
    for unit in deleted:
        rows.append({"unit": unit, "deleted": True, **found.get(unit, {"error": "no destroy plan"})})

    failed = [row["unit"] for row in rows if row.get("error")]
    has_changes = any(row.get(key) for row in rows for key in ("add", "change", "replace", "destroy"))
    report = table(rows)
    print(report, end="")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
            out.write("### Plan\n\n" + (report if rows else "No units were planned.\n"))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"changes={json.dumps(rows, separators=(',', ':'))}\n")
            out.write(f"has_changes={'true' if has_changes else 'false'}\n")
            out.write("plans<<PLAN_REPORT_EOF\n" + "\n".join(plans) + "\nPLAN_REPORT_EOF\n")
    if failed:
        print(f"::error title=Missing plans::{', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
