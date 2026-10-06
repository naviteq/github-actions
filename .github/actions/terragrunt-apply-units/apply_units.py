#!/usr/bin/env python3
"""Check an approval against the saved plans, then apply exactly those plans.

`approval` validates an `/approve apply-<id>` comment on a gate issue; `plans` checks the
downloaded metadata.json against that approval, or against an expectations policy; `run`
re-plans each unit and compares changesets (`check`), applies the saved plan files
(`apply`), or proves a second plan is empty (`clean`). It lives beside its action, not in
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
from pathlib import Path

MARKER = re.compile(r"<!-- terragrunt-apply: (\{.*?\}) -->")
APPROVE = re.compile(r"^\s*/approve\s+apply-(\d+)\s*$")
COUNT_KEYS = ("add", "change", "replace", "destroy")


class Refused(Exception):
    """A reason not to apply, shown to whoever approved."""


def resources(plan: dict) -> list[dict[str, str]]:
    """Same as terragrunt-plan-report's: address and action per changing resource."""
    found = []
    for change in plan.get("resource_changes") or []:
        actions = change.get("change", {}).get("actions", [])
        if "create" in actions and "delete" in actions:
            action = "replace"
        elif actions in (["create"], ["update"], ["delete"]):
            action = actions[0]
        else:
            continue
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


def check_expectations(metadata: dict, expect: dict) -> None:
    """An automatic apply's policy: which actions it may take, and optionally exact counts."""
    known = {"allow_replace", "allow_destroy", "units", "require_changes"}
    unknown = sorted(set(expect) - known)
    if unknown:
        raise Refused(f"expect has unknown keys: {', '.join(unknown)}")
    if metadata.get("deleted"):
        raise Refused("the plan deletes units, which an automatic apply never does: "
                      + ", ".join(metadata["deleted"]))
    units = {u["unit"]: u for u in metadata.get("units", [])}
    for unit in units.values():
        if unit.get("replace") and not expect.get("allow_replace", False):
            raise Refused(f"{unit['unit']} would replace {unit['replace']} resources and allow_replace is off")
        if unit.get("destroy") and not expect.get("allow_destroy", False):
            raise Refused(f"{unit['unit']} would destroy {unit['destroy']} resources and allow_destroy is off")
    for name, counts in (expect.get("units") or {}).items():
        if name not in units:
            raise Refused(f"expected unit {name} was not planned")
        for key, wanted in counts.items():
            if key not in COUNT_KEYS:
                raise Refused(f"expect.units.{name} has unknown count {key}")
            if units[name].get(key, 0) != wanted:
                raise Refused(f"{name} would {key} {units[name].get(key, 0)}, expected {wanted}")
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
    run.add_argument("--phase", choices=["check", "apply", "clean"], required=True)
    run.add_argument("--metadata", required=True)
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
