#!/usr/bin/env python3
"""Check an approval against the saved plans, then apply exactly those plans.

`approval` validates an `/approve apply-<id>` comment on a gate issue; `plans` checks the
downloaded metadata.json against that approval, or against an expectations policy; `run`
re-plans each unit and compares changesets (`check`), applies the saved plan files
(`apply`), proves a second plan is empty (`clean`), or plans, checks and applies each
unit in dependency order with no saved plan (`fresh`). It lives beside its action, not in
scripts/, because the public mirror copies actions only. → docs/terragrunt-apply.md

    python3 .github/actions/terragrunt-apply-units/apply_units.py approval --approvers "$APPROVERS"
    python3 .github/actions/terragrunt-apply-units/apply_units.py run --phase check --units "$UNITS" ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MARKER = re.compile(r"<!-- terragrunt-apply: (\{.*?\}) -->")
APPROVE = re.compile(r"^\s*/approve\s+apply-(\d+)\s*$")
COUNT_KEYS = ("add", "change", "replace", "destroy", "outputs")


class Refused(Exception):
    """A reason not to apply, shown to whoever approved."""


def action_of(actions: list[str]) -> str | None:
    if "create" in actions and "delete" in actions:
        return "replace"
    if actions in (["create"], ["update"], ["delete"]):
        return actions[0]
    return None


def resources(plan: dict) -> list[dict[str, str]]:
    """Same as terragrunt-plan-report's: address and action per changing resource and root output."""
    found = []
    for name, change in (plan.get("output_changes") or {}).items():
        action = action_of(change.get("actions", []))
        if action:
            found.append({"address": f"output.{name}", "action": action})
    for change in plan.get("resource_changes") or []:
        action = action_of(change.get("change", {}).get("actions", []))
        if action:
            found.append({"address": change.get("address", ""), "action": action})
    return sorted(found, key=lambda r: r["address"])


def changeset(found: list[dict[str, str]]) -> str:
    text = "\n".join(f"{r['address']} {r['action']}" for r in found)
    return hashlib.sha256(text.encode()).hexdigest()


def approvers_of(value: str) -> list[str]:
    return [login.strip().lstrip("@").lower() for login in value.replace("\n", ",").split(",") if login.strip()]


def check_approval(event: dict, approvers: list[str], gate_login: str, label: str) -> dict:
    """The gate's marker from an approved issue; raises Refused otherwise."""
    comment, issue = event.get("comment") or {}, event.get("issue") or {}
    if event.get("action") != "created" or not comment:
        raise Refused("this run was not started by a new issue comment")
    match = APPROVE.match(comment.get("body") or "")
    if not match:
        raise Refused("the comment is not exactly `/approve apply-<artifact id>`")
    actor = (comment.get("user") or {}).get("login", "")
    if not approvers:
        raise Refused("no approvers are configured, so nobody can approve")
    if actor.lower() not in approvers:
        raise Refused(f"@{actor} is not one of the approvers")
    if (issue.get("user") or {}).get("login") != gate_login:
        raise Refused(f"the issue was not opened by the gate ({gate_login})")
    labels = {item.get("name") for item in issue.get("labels") or []}
    if f"{label}:pending" not in labels:
        raise Refused(f"the issue is not labelled `{label}:pending`; it was applied, refused or closed already")
    found = MARKER.search(issue.get("body") or "")
    if not found:
        raise Refused("the issue carries no gate marker")
    marker = json.loads(found.group(1))
    if str(marker.get("artifact_id")) != match.group(1):
        raise Refused(f"the comment approves artifact {match.group(1)}, the issue gates {marker.get('artifact_id')}")
    return {**marker, "actor": actor, "issue": issue.get("number")}


def check_plans(metadata: dict, marker: dict | None, artifact: dict | None, commit_tree: str) -> None:
    """The downloaded plans are the ones approved, for the commit about to be checked out."""
    if metadata.get("schema") != 1:
        raise Refused(f"metadata.json schema {metadata.get('schema')!r} is not one this apply reads")
    if metadata.get("tree") != commit_tree:
        raise Refused("the checked-out code differs from the code that was planned")
    if marker is None:
        return
    if artifact is None or artifact.get("expired"):
        raise Refused("the plan artifact expired; re-plan the change")
    if (artifact.get("workflow_run") or {}).get("id") != marker["plan_run_id"]:
        raise Refused("the artifact does not come from the plan run the gate recorded")
    for key in ("plan_run_id", "head_sha", "tree", "pr"):
        if metadata.get(key) != marker.get(key):
            raise Refused(f"metadata.json {key} {metadata.get(key)!r} differs from the gate's {marker.get(key)!r}")


def check_policy(expect: dict) -> None:
    """Typos in a policy fail before anything is planned."""
    known = {"allow_replace", "allow_destroy", "units", "require_changes"}
    unknown = sorted(set(expect) - known)
    if unknown:
        raise Refused(f"expect has unknown keys: {', '.join(unknown)}")
    for name, counts in (expect.get("units") or {}).items():
        bad = sorted(set(counts) - set(COUNT_KEYS))
        if bad:
            raise Refused(f"expect.units.{name} has unknown counts: {', '.join(bad)}")


def unit_violation(unit: dict, expect: dict) -> str:
    """Why one unit's counts break the policy; empty when they do not."""
    if unit.get("replace") and not expect.get("allow_replace", False):
        return f"{unit['unit']} would replace {unit['replace']} resources and allow_replace is off"
    if unit.get("destroy") and not expect.get("allow_destroy", False):
        return f"{unit['unit']} would destroy {unit['destroy']} resources and allow_destroy is off"
    for key, wanted in ((expect.get("units") or {}).get(unit["unit"]) or {}).items():
        if unit.get(key, 0) != wanted:
            return f"{unit['unit']} would {key} {unit.get(key, 0)}, expected {wanted}"
    return ""


def check_expectations(metadata: dict, expect: dict) -> None:
    """An automatic apply's policy: which actions it may take, and optionally exact counts."""
    check_policy(expect)
    if metadata.get("deleted"):
        raise Refused("the plan deletes units, which an automatic apply never does: "
                      + ", ".join(metadata["deleted"]))
    units = {u["unit"]: u for u in metadata.get("units", [])}
    for unit in units.values():
        violation = unit_violation(unit, expect)
        if violation:
            raise Refused(violation)
    for name in expect.get("units") or {}:
        if name not in units:
            raise Refused(f"expected unit {name} was not planned")
    if expect.get("require_changes") and not metadata.get("has_changes"):
        raise Refused("the plan changes nothing and require_changes is on")


def find_newest(unit_dir: Path, name: str) -> Path | None:
    found = sorted(unit_dir.glob(f".terragrunt-cache/**/{name}"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def terragrunt(unit_dir: Path, *args: str) -> int:
    print(f"::group::{unit_dir}: {' '.join(args)}", flush=True)
    code = subprocess.run(["terragrunt", "run", "--non-interactive", "--", *args], cwd=unit_dir, check=False).returncode
    print("::endgroup::", flush=True)
    return code


def run_check(root: Path, units: list[dict], engine: str) -> None:
    """Re-plan every unit before anything is applied; any difference is drift."""
    drifted = []
    for unit in units:
        unit_dir = root / unit["unit"]
        if terragrunt(unit_dir, "plan", "-lock=false", "-input=false", "-out=tfplan-check") != 0:
            raise Refused(f"{unit['unit']} could not be planned again")
        plan_file = find_newest(unit_dir, "tfplan-check")
        if plan_file is None:
            raise Refused(f"{unit['unit']} left no plan to compare")
        shown = subprocess.run([engine, "show", "-json", plan_file.name], cwd=plan_file.parent,
                               capture_output=True, text=True, check=True)
        now = changeset(resources(json.loads(shown.stdout)))
        print(f"{unit['unit']}: {'unchanged' if now == unit['changeset'] else 'DRIFTED'} since the plan")
        if now != unit["changeset"]:
            drifted.append(unit["unit"])
    if drifted:
        raise Refused("these units would now change differently than planned: " + ", ".join(drifted)
                      + ". Re-plan them")


def run_apply(root: Path, units: list[dict], plans_dir: Path) -> None:
    for unit in units:
        plan_file = plans_dir / unit["file"].removesuffix(".age")
        if not plan_file.is_file():
            raise Refused(f"{unit['unit']} has no decrypted plan at {plan_file}")
        if terragrunt(root / unit["unit"], "apply", "-input=false", str(plan_file.resolve())) != 0:
            raise Refused(f"{unit['unit']} failed to apply; the units after it were not touched")
        print(f"{unit['unit']}: applied")


def run_clean(root: Path, units: list[dict]) -> None:
    """After an automatic apply, a second plan must change nothing."""
    dirty = []
    for unit in units:
        code = terragrunt(root / unit["unit"], "plan", "-lock=false", "-input=false", "-detailed-exitcode")
        if code == 2:
            dirty.append(unit["unit"])
        elif code != 0:
            raise Refused(f"{unit['unit']} could not be planned after the apply")
    if dirty:
        raise Refused("still not converged after the apply: " + ", ".join(dirty))


def counts_of(found: list[dict[str, str]]) -> dict[str, int]:
    actions = [r["action"] for r in found if not r["address"].startswith("output.")]
    return {"add": actions.count("create"), "change": actions.count("update"),
            "replace": actions.count("replace"), "destroy": actions.count("delete"),
            "outputs": len(found) - len(actions)}


def parse_dependencies(listing: str, wanted: list[str]) -> dict[str, set[str]]:
    """Each wanted unit's dependencies among the wanted units, from `terragrunt list --long --dependencies`."""
    chosen = set(wanted)
    found: dict[str, set[str]] = {unit: set() for unit in wanted}
    for line in listing.splitlines()[1:]:
        fields = line.split(None, 2)
        if len(fields) < 2 or fields[0] != "unit" or fields[1] not in chosen:
            continue
        deps = [d.strip() for d in (fields[2] if len(fields) > 2 else "").split(",") if d.strip()]
        found[fields[1]] = {d for d in deps if d in chosen}
    return found


def levels(order: list[str], deps: dict[str, set[str]]) -> list[list[str]]:
    """Groups that can run together: a unit's level is one past its deepest dependency's."""
    depth: dict[str, int] = {}

    def of(unit: str, seen: frozenset = frozenset()) -> int:
        if unit in seen:
            raise Refused(f"dependency cycle through {unit}")
        if unit not in depth:
            depth[unit] = 1 + max((of(d, seen | {unit}) for d in deps.get(unit, ())), default=-1)
        return depth[unit]

    grouped: dict[int, list[str]] = {}
    for unit in order:
        grouped.setdefault(of(unit), []).append(unit)
    return [grouped[level] for level in sorted(grouped)]


_PRINT = threading.Lock()


def streamed(unit: str, cwd: Path, *args: str) -> int:
    """terragrunt run in one unit, its output prefixed so parallel units stay readable."""
    process = subprocess.Popen(["terragrunt", "run", "--non-interactive", "--", *args], cwd=cwd,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert process.stdout is not None
    for line in process.stdout:
        with _PRINT:
            print(f"[{unit}] {line}", end="", flush=True)
    return process.wait()


def fresh_unit(root: Path, unit: str, expect: dict, engine: str) -> dict:
    """Plan one unit now, check the plan against the policy, then apply exactly that plan."""
    unit_dir = root / unit
    if streamed(unit, unit_dir, "plan", "-input=false", "-out=tfplan-fresh") != 0:
        return {"unit": unit, "outcome": "plan failed"}
    plan_file = find_newest(unit_dir, "tfplan-fresh")
    if plan_file is None:
        return {"unit": unit, "outcome": "left no plan"}
    shown = subprocess.run([engine, "show", "-json", plan_file.name], cwd=plan_file.parent,
                           capture_output=True, text=True, check=True)
    row = {"unit": unit, **counts_of(resources(json.loads(shown.stdout)))}
    violation = unit_violation(row, expect)
    if violation:
        return {**row, "outcome": "refused", "reason": violation}
    if not any(row[key] for key in COUNT_KEYS):
        return {**row, "outcome": "unchanged"}
    if streamed(unit, unit_dir, "apply", "-input=false", str(plan_file.resolve())) != 0:
        return {**row, "outcome": "apply failed"}
    return {**row, "outcome": "applied"}


def run_fresh(root: Path, order: list[str], expect: dict, engine: str, parallelism: int) -> list[dict]:
    """Level by level in dependency order; a level that fails anywhere is the last one run."""
    check_policy(expect)
    listing = subprocess.run(["terragrunt", "list", "--long", "--dependencies", "--queue-construct-as", "apply"],
                             cwd=root.resolve(), capture_output=True, text=True, check=True).stdout
    rows: list[dict] = []
    for group in levels(order, parse_dependencies(listing, order)):
        with ThreadPoolExecutor(max_workers=max(1, parallelism)) as pool:
            done = list(pool.map(lambda unit: fresh_unit(root, unit, expect, engine), group))
        rows.extend(done)
        if any(row["outcome"] not in ("applied", "unchanged") for row in done):
            break
    return rows


def fresh_report(rows: list[dict], order: list[str]) -> str:
    lines = ["### Fresh apply", "", "| Unit | Add | Change | Replace | Destroy | Outputs | Outcome |",
             "|---|---|---|---|---|---|---|"]
    for row in rows:
        counts = " | ".join(str(row.get(key, "")) for key in COUNT_KEYS)
        lines.append(f"| `{row['unit']}` | {counts} | {row.get('reason') or row['outcome']} |")
    reached = {row["unit"] for row in rows}
    for unit in order:
        if unit not in reached:
            lines.append(f"| `{unit}` | | | | | | not reached |")
    return "\n".join(lines) + "\n"


def write_outputs(**values: object) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            for key, value in values.items():
                out.write(f"{key}={value}\n")


def refuse(reason: str) -> int:
    print(f"::error title=Apply refused::{reason}")
    write_outputs(reason=reason)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    approval = sub.add_parser("approval")
    approval.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH", ""))
    approval.add_argument("--approvers", default="")
    approval.add_argument("--gate-login", default="github-actions[bot]")
    approval.add_argument("--label", default="terragrunt-apply")
    plans = sub.add_parser("plans")
    plans.add_argument("--metadata", required=True)
    plans.add_argument("--commit-tree", required=True)
    plans.add_argument("--marker-json", default="")
    plans.add_argument("--artifact-json", default="")
    plans.add_argument("--expect-json", default="")
    run = sub.add_parser("run")
    run.add_argument("--phase", choices=["check", "apply", "clean", "fresh"], required=True)
    run.add_argument("--metadata", default="")
    run.add_argument("--expect-json", default="")
    run.add_argument("--parallelism", type=int, default=4)
    run.add_argument("--units", default="", help="This job's units, one per line; empty means all")
    run.add_argument("--working-directory", default=".")
    run.add_argument("--plans-dir", default="")
    run.add_argument("--engine", default="tofu")
    args = parser.parse_args(argv)

    try:
        if args.command == "approval":
            event = json.loads(Path(args.event).read_text(encoding="utf-8"))
            marker = check_approval(event, approvers_of(args.approvers), args.gate_login, args.label)
            print(f"@{marker['actor']} approved artifact {marker['artifact_id']} of PR #{marker['pr']}")
            write_outputs(marker=json.dumps(marker, separators=(",", ":")), artifact_id=marker["artifact_id"],
                          plan_run_id=marker["plan_run_id"], commit=marker["commit"], issue=marker["issue"])
            return 0
        if args.command == "run" and args.phase == "fresh":
            order = [line.strip() for line in args.units.splitlines() if line.strip()]
            expect = json.loads(args.expect_json or "{}")
            rows = run_fresh(Path(args.working_directory), order, expect, args.engine, args.parallelism)
            report = fresh_report(rows, order)
            print(report, end="")
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
                    out.write(report)
            applied = [row["unit"] for row in rows if row["outcome"] == "applied"]
            write_outputs(changes=json.dumps(rows, separators=(",", ":")))
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
                    out.write("applied<<APPLY_UNITS_EOF\n" + "".join(u + "\n" for u in applied) + "APPLY_UNITS_EOF\n")
            stopped = [row for row in rows if row["outcome"] not in ("applied", "unchanged")]
            if stopped:
                raise Refused("; ".join(f"{row['unit']}: {row.get('reason') or row['outcome']}" for row in stopped)
                              + ". Units after these were not reached")
            if expect.get("require_changes") and not applied:
                raise Refused("nothing changed and require_changes is on")
            return 0
        if args.command == "run" and args.phase == "clean" and not args.metadata:
            run_clean(Path(args.working_directory),
                      [{"unit": line.strip()} for line in args.units.splitlines() if line.strip()])
            return 0
        metadata = json.loads(Path(args.metadata).read_text(encoding="utf-8"))
        if args.command == "plans":
            marker = json.loads(args.marker_json) if args.marker_json else None
            artifact = json.loads(args.artifact_json) if args.artifact_json else None
            check_plans(metadata, marker, artifact, args.commit_tree)
            if marker is None:
                check_expectations(metadata, json.loads(args.expect_json or "{}"))
            units = [u["unit"] for u in metadata["units"] if any(u.get(k) for k in COUNT_KEYS)]
            print(f"{len(units)} units to apply: {', '.join(units) or 'none'}")
            write_outputs(has_changes=str(bool(units)).lower())
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
                    out.write("units<<APPLY_UNITS_EOF\n" + "".join(u + "\n" for u in units) + "APPLY_UNITS_EOF\n")
            return 0
        wanted = {line.strip() for line in args.units.splitlines() if line.strip()}
        units = [u for u in metadata["units"] if any(u.get(k) for k in COUNT_KEYS)
                 and (not wanted or u["unit"] in wanted)]
        root = Path(args.working_directory)
        if args.phase == "check":
            run_check(root, units, args.engine)
        elif args.phase == "apply":
            run_apply(root, units, Path(args.plans_dir))
        else:
            run_clean(root, units)
        return 0
    except Refused as reason:
        return refuse(str(reason))


if __name__ == "__main__":
    sys.exit(main())
