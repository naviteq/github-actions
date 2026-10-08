#!/usr/bin/env python3
"""Count what each Terragrunt plan would do, and describe each change without its secrets.

Reads the tfplan every planned unit left in its .terragrunt-cache, and the destroy plans
of deleted units from the run's log. Units a destroy run kept for prevent_destroy have no
plan and are reported as kept. The attribute-level diff it writes is the only place values
leave the plan job, so masking happens here and nowhere else. → docs/terragrunt-plan.md

    python3 .github/actions/terragrunt-plan-report/report.py --working-directory terragrunt \\
        --engine tofu --units "$UNITS" --deleted "$DELETED" --destroy-log destroy.log
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
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
    """Tally resource_changes by action; a replace is counted once, as a replace. outputs
    counts changed root outputs: a unit whose only change is an output still needs an apply,
    or its dependents keep reading the old value, or their mocks."""
    counts = {"add": 0, "change": 0, "replace": 0, "destroy": 0, "outputs": len(output_changes(plan))}
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


def action_of(actions: list[str]) -> str | None:
    """One word per change, as the apply compares them; None for no-op and read."""
    if "create" in actions and "delete" in actions:
        return "replace"
    if actions in (["create"], ["update"], ["delete"]):
        return actions[0]
    return None


def output_changes(plan: dict) -> list[dict[str, str]]:
    """Root outputs the plan would change, addressed as output.<name>."""
    found = []
    for name, change in (plan.get("output_changes") or {}).items():
        action = action_of(change.get("actions", []))
        if action:
            found.append({"address": f"output.{name}", "action": action})
    return found


def resources(plan: dict) -> list[dict[str, str]]:
    """Address and action of every resource and output the plan would change, sorted by address."""
    found = output_changes(plan)
    for change in plan.get("resource_changes") or []:
        action = action_of(change.get("change", {}).get("actions", []))
        if action:
            found.append({"address": change.get("address", ""), "action": action})
    return sorted(found, key=lambda r: r["address"])


def changeset(found: list[dict[str, str]]) -> str:
    """Hash of what would change, never of values; the apply re-plans and compares it."""
    text = "\n".join(f"{r['address']} {r['action']}" for r in found)
    return hashlib.sha256(text.encode()).hexdigest()


DEFAULT_MASK = ("*password*", "*secret*", "*token*", "private_key", "user_data", "kube_config",
                "helm_release.values")
SENSITIVE = "(sensitive value)"
MASKED = "(masked)"
UNKNOWN = "(known after apply)"
VALUE_LIMIT = 160
LINES_PER_RESOURCE = 40
ABSENT = object()


def flatten(value: object, parts: tuple = ()) -> list[tuple[tuple, object]]:
    """Leaf paths of a plan value; an empty map or list is a leaf of its own."""
    if isinstance(value, dict) and value:
        return [leaf for key, item in value.items() for leaf in flatten(item, parts + (key,))]
    if isinstance(value, list) and value:
        return [leaf for index, item in enumerate(value) for leaf in flatten(item, parts + (index,))]
    return [(parts, value)] if parts else []


def marked(marks: object, parts: tuple) -> bool:
    """True when a sensitive or unknown structure marks this path or one of its parents."""
    node = marks
    for part in parts:
        if node is True:
            return True
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and isinstance(part, int) and part < len(node):
            node = node[part]
        else:
            return False
    return node is True


def path_text(parts: tuple) -> str:
    text = ""
    for part in parts:
        text += f"[{part}]" if isinstance(part, int) else (f".{part}" if text else str(part))
    return text


def masked_name(parts: tuple, resource_type: str, patterns: list[str]) -> bool:
    names = [path_text(parts)]
    if parts:
        names += [str(parts[0]), f"{resource_type}.{parts[0]}"]
        names += [str(part) for part in parts if isinstance(part, str)]
    return any(fnmatch.fnmatchcase(name.lower(), pattern.lower()) for name in names for pattern in patterns)


def normalise(value: object) -> object:
    """A JSON document held in a string compares, and renders, by its content."""
    if isinstance(value, str) and value.strip()[:1] in ("{", "["):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def show(value: object) -> str:
    value = normalise(value)
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) \
        if not isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= VALUE_LIMIT else text[:VALUE_LIMIT - 1] + "…"


def describe(value: object, parts: tuple, sensitive: object, unknown: object, hidden: bool) -> str:
    if marked(sensitive, parts):
        return SENSITIVE
    if hidden:
        return MASKED
    if marked(unknown, parts):
        return UNKNOWN
    return show(value)


def resource_lines(change: dict, resource_type: str, patterns: list[str]) -> list[list[str]]:
    """[sign, text] per changed attribute of one resource, the way `show` reads, without secrets."""
    actions = change.get("actions", [])
    if actions == ["delete"]:
        return []
    creating = actions == ["create"]
    before = {} if creating else (change.get("before") or {})
    after = change.get("after") or {}
    before_sensitive, after_sensitive = change.get("before_sensitive"), change.get("after_sensitive")
    unknown = change.get("after_unknown")
    replace_paths = {tuple(path) for path in change.get("replace_paths") or []}
    old = dict(flatten(before))
    new = dict(flatten(after))
    unknown_paths = {parts for parts, value in flatten(unknown) if value is True} if isinstance(unknown, dict) else set()
    lines: list[list[str]] = []
    for parts in sorted(set(old) | set(new) | unknown_paths, key=path_text):
        was, now = old.get(parts, ABSENT), new.get(parts, ABSENT)
        pending = marked(unknown, parts)
        if creating and (now in (None, ABSENT) or now == [] or now == {}) and not pending:
            continue
        if not creating and not pending and normalise(was) == normalise(now):
            continue
        hidden = masked_name(parts, resource_type, patterns)
        name = path_text(parts)
        forces = "  # forces replacement" if any(parts[:len(r)] == r for r in replace_paths) else ""
        after_text = describe(now, parts, after_sensitive, unknown, hidden)
        if creating or was in (None, ABSENT):
            lines.append(["+", f"{name} = {after_text}{forces}"])
        elif now is ABSENT or (now is None and not pending):
            lines.append(["-", f"{name} = {describe(was, parts, before_sensitive, None, hidden)}{forces}"])
        else:
            lines.append(["~", f"{name} = {describe(was, parts, before_sensitive, None, hidden)} → {after_text}{forces}"])
    if len(lines) > LINES_PER_RESOURCE:
        lines = lines[:LINES_PER_RESOURCE] + [[" ", f"… {len(lines) - LINES_PER_RESOURCE} more attributes in the run"]]
    return lines


def output_lines(name: str, change: dict, patterns: list[str]) -> list[list[str]]:
    """An output diffs like an attribute named after it, so a map output shows only the keys that change."""
    if change.get("before_sensitive") or change.get("after_sensitive"):
        return [["~", f"{name} = {SENSITIVE}"]]
    wrapped = {"actions": change.get("actions", []),
               "before": {name: change.get("before")}, "after": {name: change.get("after")},
               "after_unknown": {name: change.get("after_unknown")} if change.get("after_unknown") else None}
    return resource_lines(wrapped, "output", patterns) or [["~", f"{name} = {show(change.get('after'))}"]]


def diffs(plan: dict, patterns: list[str]) -> dict[str, list[list[str]]]:
    """Address → the sanitised lines of its change, for every resource and root output that changes."""
    found: dict[str, list[list[str]]] = {}
    for name, change in (plan.get("output_changes") or {}).items():
        if action_of(change.get("actions", [])):
            found[f"output.{name}"] = output_lines(name, change, patterns)
    for change in plan.get("resource_changes") or []:
        if action_of(change.get("change", {}).get("actions", [])):
            found[change.get("address", "")] = resource_lines(change.get("change", {}), change.get("type", ""), patterns)
    return found


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
    lines = ["| Unit | Add | Change | Replace | Destroy | Outputs |", "|---|---|---|---|---|---|"]
    for row in rows:
        unit = f"`{row['unit']}`" + (" (deleted)" if row.get("deleted") else "")
        if row.get("kept"):
            lines.append(f"| {unit} | kept: prevent_destroy | | | | |")
            continue
        if row.get("error"):
            lines.append(f"| {unit} | {row['error']} | | | | |")
            continue
        lines.append(f"| {unit} | {row['add']} | {row['change']} | {row['replace']} | {row['destroy']} | {row.get('outputs', 0)} |")
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
    parser.add_argument("--kept", default="", help="Units a destroy run left out for prevent_destroy, one per line")
    parser.add_argument("--resources-file", default="", help="Write each unit's changed addresses, actions and diffs here")
    parser.add_argument("--mask", default="\n".join(DEFAULT_MASK),
                        help="Attribute name globs whose values never render, one per line or comma-separated")
    parser.add_argument("--secret-units", default="", help="Unit path globs whose diffs show addresses only")
    args = parser.parse_args(argv)

    root = Path(args.working_directory)
    patterns = [item.strip() for item in args.mask.replace(",", "\n").splitlines() if item.strip()]
    secret_globs = [item.strip() for item in args.secret_units.replace(",", "\n").splitlines() if item.strip()]

    def secret(unit: str) -> bool:
        return any(fnmatch.fnmatchcase(unit, glob) for glob in secret_globs)

    rows: list[dict] = []
    plans: list[str] = []
    detail: dict[str, list[dict[str, str]]] = {}
    kept = set(_lines(args.kept))
    for unit in _lines(args.units):
        if unit in kept:
            rows.append({"unit": unit, "kept": True})
            continue
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
        plan = json.loads(shown.stdout)
        if plan.get("errored"):
            # Terraform and OpenTofu save the plan even when planning fails; its partial
            # changes would read as a clean unit.
            rows.append({"unit": unit, "error": "plan failed"})
            continue
        changed = diffs(plan, patterns) if not secret(unit) else {}
        detail[unit] = [{**found, "lines": changed.get(found["address"], [])} for found in resources(plan)]
        rows.append({"unit": unit, **count_actions(plan), "changeset": changeset(resources(plan))})
        plans.append(f"{unit}\t{plan_file}")

    deleted = _lines(args.deleted)
    log_file = Path(args.destroy_log) if args.destroy_log else None
    log = log_file.read_text(encoding="utf-8") if log_file and log_file.is_file() else ""
    found = destroy_counts(log, deleted)
    for unit in deleted:
        rows.append({"unit": unit, "deleted": True, **found.get(unit, {"error": "no destroy plan"})})

    failed = [row["unit"] for row in rows if row.get("error")]
    has_changes = any(row.get(key) for row in rows for key in ("add", "change", "replace", "destroy", "outputs"))
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
    if args.resources_file:
        Path(args.resources_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.resources_file).write_text(json.dumps(detail, separators=(",", ":")), encoding="utf-8")
    if failed:
        print(f"::error title=Missing plans::{', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
