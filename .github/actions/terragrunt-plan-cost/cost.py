#!/usr/bin/env python3
"""Estimate what each planned unit changes in monthly cost, with Infracost.

Runs `infracost breakdown` on each unit's `show -json`, which stays on the runner, and keeps
only the monthly totals before and after, their delta, and the resources whose cost moves
most. A unit Infracost prices nothing in (OCI, unsupported resources) gets no estimate rather
than a zero. It lives beside its action, not in scripts/, because the public mirror copies
actions only. → docs/terragrunt-plan.md

    python3 .github/actions/terragrunt-plan-cost/cost.py --plans "$PLANS" --engine tofu --out costs.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Results never go to Infracost Cloud; the pricing API only answers price lookups.
QUIET_ENV = {"INFRACOST_ENABLE_CLOUD": "false", "INFRACOST_ENABLE_CLOUD_UPLOAD": "false",
             "INFRACOST_ENABLE_DASHBOARD": "false", "INFRACOST_SKIP_UPDATE_CHECK": "true",
             "INFRACOST_NO_COLOR": "true"}
TOP = 5


def money(value: object) -> float | None:
    return None if value in (None, "") else round(float(value), 2)


def summarise(output: dict) -> dict:
    """The unit's estimate from Infracost's JSON: totals, delta and the biggest movers only."""
    projects = output.get("projects") or []
    supported = sum((p.get("summary") or {}).get("totalSupportedResources", 0) for p in projects)
    unsupported = sum((p.get("summary") or {}).get("totalUnsupportedResources", 0) for p in projects)
    currency = output.get("currency") or "USD"
    # Free resources (IAM, random) cost a true zero; resources Infracost cannot price (OCI) are unknown.
    if not supported and unsupported:
        return {"estimate": False, "reason": "not supported by Infracost", "currency": currency}
    movers = []
    for project in projects:
        for resource in (project.get("diff") or {}).get("resources") or []:
            delta = money(resource.get("monthlyCost"))
            if delta:
                movers.append({"address": resource.get("name", ""), "delta": delta})
    movers.sort(key=lambda m: (-abs(m["delta"]), m["address"]))
    return {"estimate": True, "currency": currency,
            "before": money(output.get("pastTotalMonthlyCost")) or 0.0,
            "after": money(output.get("totalMonthlyCost")) or 0.0,
            "delta": money(output.get("diffTotalMonthlyCost")) or 0.0,
            "unpriced": unsupported, "top": movers[:TOP]}


def estimate(unit: str, plan_file: Path, engine: str, infracost: str) -> dict:
    shown = subprocess.run([engine, "show", "-json", plan_file.name], cwd=plan_file.parent,
                           capture_output=True, text=True, check=False)
    if shown.returncode != 0:
        return {"estimate": False, "reason": "plan unreadable"}
    with tempfile.TemporaryDirectory() as tmp:
        plan_json, out = Path(tmp) / "plan.json", Path(tmp) / "cost.json"
        plan_json.write_text(shown.stdout, encoding="utf-8")
        ran = subprocess.run([infracost, "breakdown", "--path", str(plan_json), "--format", "json",
                              "--out-file", str(out)], capture_output=True, text=True, check=False,
                             env={**os.environ, **QUIET_ENV})
        if ran.returncode != 0 or not out.is_file():
            print(f"::warning title=No cost estimate::{unit}: infracost exited {ran.returncode}")
            return {"estimate": False, "reason": "infracost failed"}
        return summarise(json.loads(out.read_text(encoding="utf-8")))


def table(costs: dict[str, dict]) -> str:
    lines = ["| Unit | Before | After | Change |", "|---|--:|--:|--:|"]
    for unit, cost in costs.items():
        if cost.get("estimate"):
            lines.append(f"| `{unit}` | {cost['before']:,.2f} | {cost['after']:,.2f} | {cost['delta']:+,.2f} |")
        else:
            lines.append(f"| `{unit}` | | | no estimate ({cost.get('reason', '')}) |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plans", default="", help="unit<TAB>plan file lines, as the plan report outputs them")
    parser.add_argument("--engine", default="tofu")
    parser.add_argument("--infracost", default="infracost")
    parser.add_argument("--out", required=True, help="Write each unit's estimate here, as JSON")
    args = parser.parse_args(argv)

    costs: dict[str, dict] = {}
    for line in args.plans.splitlines():
        if "\t" not in line:
            continue
        unit, plan_file = line.split("\t", 1)
        costs[unit] = estimate(unit, Path(plan_file), args.engine, args.infracost)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(costs, separators=(",", ":")), encoding="utf-8")
    report = table(costs) if costs else "No unit was planned.\n"
    print(report, end="")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
            out.write("### Monthly cost\n\n" + report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
