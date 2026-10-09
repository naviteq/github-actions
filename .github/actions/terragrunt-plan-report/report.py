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
import difflib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
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
INDENT = 4
ABSENT = object()
HEADLINE = {"create": "will be created", "update": "will be updated in-place", "replace": "must be replaced",
            "delete": "will be destroyed"}
# The plan always shows these on an update, so a reader can tell which object it is.
IDENTIFYING = ("id", "name")


@dataclass
class Change:
    """What one resource or output change knows about its values, beside the values."""
    before_sensitive: object = None
    after_sensitive: object = None
    unknown: object = None
    replace_paths: set = field(default_factory=set)
    resource_type: str = ""
    patterns: list = field(default_factory=list)
    creating: bool = False


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


def node_at(marks: object, parts: tuple) -> object:
    for part in parts:
        if isinstance(marks, dict):
            marks = marks.get(part)
        elif isinstance(marks, list) and isinstance(part, int) and part < len(marks):
            marks = marks[part]
        else:
            return None
    return marks


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


def literal(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text if len(text) <= VALUE_LIMIT else text[:VALUE_LIMIT - 1] + "…"


def empty(value: object) -> bool:
    """Nothing worth a line: absent, null, or an empty list or map."""
    return value is ABSENT or value is None or value == [] or value == {}


def missing(value: object) -> bool:
    return value is ABSENT or value is None


def noun(word: str, count: int) -> str:
    return word if count == 1 else word + "s"


def line(sign: str, depth: int, text: str) -> list[str]:
    """One line laid out as the plan prints it, with its sign moved to the first column."""
    return [sign, " " * (INDENT * (depth + 1) - len(sign)) + text]


def shown(ch: Change, parts: tuple, was: object, now: object) -> bool:
    if marked(ch.unknown, parts):
        return True
    was, now = normalise(was), normalise(now)
    if empty(now) and (ch.creating or empty(was)):
        return False
    return ch.creating or was != now


def leaf_value(ch: Change, parts: tuple, value: object, after: bool) -> str:
    if marked(ch.after_sensitive if after else ch.before_sensitive, parts):
        return SENSITIVE
    if masked_name(parts, ch.resource_type, ch.patterns):
        return MASKED
    if after and marked(ch.unknown, parts):
        return UNKNOWN
    return literal(normalise(value))


def attribute(ch: Change, label: str, before: tuple, after: tuple, was: object, now: object,
              depth: int) -> list[list[str]]:
    """The lines of one changed attribute: a value, or a map, list or JSON document opened up."""
    pending = marked(ch.unknown, after)
    sign = "+" if ch.creating or missing(was) else ("-" if missing(now) and not pending else "~")
    forces = " # forces replacement" if after in ch.replace_paths else ""
    head = f"{label} = " if label else ""
    tail = "" if label else ","
    old, new = normalise(was), normalise(now)
    kinds = {type(value) for value in (old, new) if not missing(value)}
    opaque = (pending or masked_name(after, ch.resource_type, ch.patterns)
              or marked(ch.before_sensitive, before) or marked(ch.after_sensitive, after))
    if not opaque and kinds in ({dict}, {list}):
        json_text = any(isinstance(value, str) and not isinstance(normalise(value), str) for value in (was, now))
        opener, closer = ("{", "}") if kinds == {dict} else ("[", "]")
        if json_text:
            opener, closer = f"jsonencode({opener}", f"{closer})"
        body = children(ch, before, after, old if not missing(old) else ABSENT, new if not missing(new) else ABSENT,
                        depth + 1, quote=kinds == {dict} and bool(label) and not json_text)
        return [line(sign, depth, f"{head}{opener}{forces}"), *body,
                line(" ", depth, closer + (" -> null" if sign == "-" and label else "") + tail)]
    if sign == "+":
        text = leaf_value(ch, after, now, True)
    elif sign == "-":
        text = leaf_value(ch, before, was, False) + (" -> null" if label else "")
    else:
        old_text, new_text = leaf_value(ch, before, was, False), leaf_value(ch, after, now, True)
        text = old_text if old_text == new_text and old_text in (SENSITIVE, MASKED) else f"{old_text} -> {new_text}"
    return [line(sign, depth, f"{head}{text}{tail}{forces}")]


def children(ch: Change, before: tuple, after: tuple, was: object, now: object, depth: int,
             quote: bool = False, root: bool = False) -> list[list[str]]:
    """The changed members of a map or list, aligned on `=`, and a count of the unchanged ones."""
    if isinstance(was, list) or isinstance(now, list):
        return elements(ch, before, after, was if isinstance(was, list) else [], now if isinstance(now, list) else [],
                        depth)
    old = was if isinstance(was, dict) else {}
    new = now if isinstance(now, dict) else {}
    pending = node_at(ch.unknown, after)
    keys = set(old) | set(new) | ({key for key, value in pending.items() if value} if isinstance(pending, dict) else set())
    members, identifying, hidden = [], [], 0
    for key in sorted(keys):
        was_item, now_item = old.get(key, ABSENT), new.get(key, ABSENT)
        if shown(ch, after + (key,), was_item, now_item):
            members.append(key)
        elif root and key in IDENTIFYING and not ch.creating and isinstance(now_item, (str, int, float)) \
                and not masked_name((key,), ch.resource_type, ch.patterns) and not marked(ch.after_sensitive, (key,)):
            identifying.append(key)
        elif not empty(was_item) or not empty(now_item):
            hidden += 1
    labels = {key: json.dumps(key) if quote else key for key in [*identifying, *members]}
    width = max((len(label) for label in labels.values()), default=0)
    lines = [line(" ", depth, f"{labels[key].ljust(width)} = {literal(new[key])}") for key in identifying]
    for key in members:
        lines += attribute(ch, labels[key].ljust(width), before + (key,), after + (key,),
                           old.get(key, ABSENT), new.get(key, ABSENT), depth)
    if hidden and not ch.creating:
        lines.append(line(" ", depth, f"# ({hidden} unchanged {noun('element' if quote else 'attribute', hidden)} hidden)"))
    return lines


def elements(ch: Change, before: tuple, after: tuple, was: list, now: list, depth: int) -> list[list[str]]:
    """A list's added, removed and changed elements, matched the way a text diff matches lines."""
    pending = node_at(ch.unknown, after)
    if isinstance(pending, list) and len(pending) > len(now):
        now = now + [None] * (len(pending) - len(now))
    keyed = difflib.SequenceMatcher(None, [literal(normalise(v)) for v in was], [literal(normalise(v)) for v in now],
                                    autojunk=False)
    lines, hidden = [], 0
    for tag, i1, i2, j1, j2 in keyed.get_opcodes():
        if tag == "equal":
            hidden += i2 - i1
            continue
        paired = tag == "replace" and i2 - i1 == j2 - j1 and all(
            isinstance(normalise(was[i]), dict) and isinstance(normalise(now[j]), dict)
            for i, j in zip(range(i1, i2), range(j1, j2)))
        if paired:
            for i, j in zip(range(i1, i2), range(j1, j2)):
                lines += attribute(ch, "", before + (i,), after + (j,), was[i], now[j], depth)
            continue
        for i in range(i1, i2):
            lines += attribute(ch, "", before + (i,), after + (i,), was[i], ABSENT, depth)
        for j in range(j1, j2):
            lines += attribute(ch, "", before + (j,), after + (j,), ABSENT, now[j], depth)
    if hidden and not ch.creating:
        lines.append(line(" ", depth, f"# ({hidden} unchanged {noun('element', hidden)} hidden)"))
    return lines


def resource_lines(resource: dict, patterns: list[str]) -> list[list[str]]:
    """[sign, text] per line of one resource's change, the way the plan prints it, without secrets."""
    change = resource.get("change", {})
    actions = change.get("actions", [])
    action = action_of(actions)
    if not action:
        return []
    ch = Change(change.get("before_sensitive"), change.get("after_sensitive"), change.get("after_unknown"),
                {tuple(path) for path in change.get("replace_paths") or []}, resource.get("type", ""), patterns,
                creating=action == "create")
    sign = {"create": "+", "update": "~", "delete": "-"}.get(action) or ("+/-" if actions[0] == "create" else "-/+")
    keyword = "data" if resource.get("mode") == "data" else "resource"
    head = [["#", f" {resource.get('address', '')} {HEADLINE[action]}"],
            line(sign, 0, f'{keyword} "{resource.get("type", "")}" "{resource.get("name", "")}" {{')]
    before = {} if action == "create" else (change.get("before") or {})
    if action == "delete":
        # A destroyed object's values say nothing a reviewer acts on, so only its identity shows.
        kept = Change(resource_type=ch.resource_type, patterns=patterns, before_sensitive=ch.before_sensitive,
                      after_sensitive=ch.before_sensitive)
        body = children(kept, (), (), before, before, 1, root=True)
        body = [entry for entry in body if not entry[1].lstrip().startswith("#")]
        hidden = sum(1 for value in before.values() if not empty(value)) - len(body)
        if hidden > 0:
            body.append(line(" ", 1, f"# ({hidden} {noun('attribute', hidden)} hidden)"))
    else:
        body = children(ch, (), (), before, change.get("after") or {}, 1, root=True)
    if len(body) > LINES_PER_RESOURCE:
        body = body[:LINES_PER_RESOURCE] + [line(" ", 1, f"# … {len(body) - LINES_PER_RESOURCE} more lines in the run")]
    return [*head, *body, line(" ", 0, "}")]


def output_lines(name: str, change: dict, patterns: list[str]) -> list[list[str]]:
    """An output reads like a top-level attribute, so a map output shows only the keys that change."""
    action = action_of(change.get("actions", []))
    if change.get("before_sensitive") or change.get("after_sensitive"):
        return [line({"create": "+", "delete": "-"}.get(action or "", "~"), 0, f"{name} = {SENSITIVE}")]
    ch = Change(unknown={name: change.get("after_unknown")} if change.get("after_unknown") else None,
                resource_type="output", patterns=patterns, creating=action == "create")
    was = ABSENT if action == "create" else change.get("before")
    now = ABSENT if action == "delete" else change.get("after")
    if not shown(ch, (name,), was, now):
        return [line("~", 0, f"{name} = {literal(now)}")]
    return attribute(ch, name, (name,), (name,), was, now, 0)


def diffs(plan: dict, patterns: list[str]) -> dict[str, list[list[str]]]:
    """Address → the sanitised lines of its change, for every resource and root output that changes."""
    found: dict[str, list[list[str]]] = {}
    for name, change in (plan.get("output_changes") or {}).items():
        if action_of(change.get("actions", [])):
            found[f"output.{name}"] = output_lines(name, change, patterns)
    for change in plan.get("resource_changes") or []:
        if action_of(change.get("change", {}).get("actions", [])):
            found[change.get("address", "")] = resource_lines(change, patterns)
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
