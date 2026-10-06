#!/usr/bin/env python3
"""Keep one `[drift] <unit>` issue per unit whose live infrastructure no longer matches its code.

Reads the plan workflow's per-unit counts and resource addresses (never values): opens or
updates the issue of every unit that would change, closes the issue of a unit that plans
clean again, and notes on its issue a unit that could not be planned. It lives beside its
action, not in scripts/, because the public mirror copies actions only. → docs/terragrunt-drift.md

    python3 .github/actions/terragrunt-drift-issues/drift.py --changes "$CHANGES" --counts-dir counts
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

COUNT_KEYS = ("add", "change", "replace", "destroy")
SIGN = {"create": "+", "update": "~", "replace": "-/+", "delete": "-"}


def gh(*args: str, payload: dict | None = None) -> object:
    command = ["gh", "api", *args]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def title_of(unit: str) -> str:
    return f"[drift] {unit}"


def classify(rows: list[dict]) -> dict[str, list[dict]]:
    """drifted, clean and failed units of this run; deleted rows are not drift."""
    found: dict[str, list[dict]] = {"drifted": [], "clean": [], "failed": []}
    for row in rows:
        if row.get("deleted"):
            continue
        if row.get("error"):
            found["failed"].append(row)
        elif any(row.get(key) for key in COUNT_KEYS):
            found["drifted"].append(row)
        else:
            found["clean"].append(row)
    return found


def body(row: dict, resources: list[dict[str, str]], run_url: str, now: str) -> str:
    lines = [f"<!-- terragrunt-drift: {row['unit']} -->",
             f"`{row['unit']}` no longer matches its code: applying it now would change the infrastructure.", "",
             "| Add | Change | Replace | Destroy |", "|---|---|---|---|",
             "| " + " | ".join(str(row.get(key, 0)) for key in COUNT_KEYS) + " |", ""]
    if resources:
        lines += ["```diff", *[f"{SIGN[r['action']]} {r['address']}" for r in resources], "```", ""]
    lines += [f"Last seen {now} in [this run]({run_url}). Either apply the code through a pull request, "
              "or change the code to match what was done by hand. The issue closes itself once the unit plans clean.", ""]
    return "\n".join(lines)


def open_issues(repo: str, label: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    page = 1
    while True:
        batch = gh(f"repos/{repo}/issues?labels={label}&state=open&per_page=100&page={page}") or []
        for issue in batch:  # type: ignore[union-attr]
            if "pull_request" not in issue:
                found[issue["title"]] = issue
        if len(batch) < 100:  # type: ignore[arg-type]
            return found
        page += 1


def reconcile(repo: str, label: str, rows: list[dict], resources: dict[str, list[dict[str, str]]],
              run_url: str, now: str) -> dict[str, list[str]]:
    """Open, update and close issues so that exactly the drifting units have one."""
    groups = classify(rows)
    existing = open_issues(repo, label)
    done: dict[str, list[str]] = {"opened": [], "updated": [], "closed": [], "failed": []}
    for row in groups["drifted"]:
        text = body(row, resources.get(row["unit"], []), run_url, now)
        issue = existing.get(title_of(row["unit"]))
        if issue:
            gh(f"repos/{repo}/issues/{issue['number']}", "--method", "PATCH", payload={"body": text})
            done["updated"].append(row["unit"])
        else:
            gh(f"repos/{repo}/issues", "--method", "POST",
               payload={"title": title_of(row["unit"]), "body": text, "labels": [label]})
            done["opened"].append(row["unit"])
    for row in groups["clean"]:
        issue = existing.get(title_of(row["unit"]))
        if issue:
            gh(f"repos/{repo}/issues/{issue['number']}/comments", "--method", "POST",
               payload={"body": f"Plans clean again as of {now} ([run]({run_url})). Closing."})
            gh(f"repos/{repo}/issues/{issue['number']}", "--method", "PATCH",
               payload={"state": "closed", "state_reason": "completed"})
            done["closed"].append(row["unit"])
    for row in groups["failed"]:
        issue = existing.get(title_of(row["unit"]))
        note = f"Could not be planned at {now} ({row['error']}); drift unknown. [Run]({run_url})."
        if issue:
            gh(f"repos/{repo}/issues/{issue['number']}/comments", "--method", "POST", payload={"body": note})
        else:
            gh(f"repos/{repo}/issues", "--method", "POST",
               payload={"title": title_of(row["unit"]), "body": f"<!-- terragrunt-drift: {row['unit']} -->\n{note}\n",
                        "labels": [label]})
        done["failed"].append(row["unit"])
    return done


def load_resources(counts_dir: Path) -> dict[str, list[dict[str, str]]]:
    found: dict[str, list[dict[str, str]]] = {}
    if counts_dir.is_dir():
        for path in sorted(counts_dir.glob("*.resources.json")):
            found.update(json.loads(path.read_text(encoding="utf-8")))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--changes", default="[]", help="The plan workflow's changes output")
    parser.add_argument("--counts-dir", default="")
    parser.add_argument("--label", default="terragrunt-drift")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--open-issues", default="true")
    args = parser.parse_args(argv)

    rows = json.loads(args.changes or "[]")
    groups = classify(rows)
    drifted = [row["unit"] for row in groups["drifted"]]
    print(f"{len(drifted)} of {len(rows)} units drifted: {', '.join(drifted) or 'none'}")
    if args.open_issues == "true":
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        done = reconcile(args.repo, args.label, rows, load_resources(Path(args.counts_dir)), args.run_url, now)
        summary = ", ".join(f"{key} {len(value)}" for key, value in done.items())
        print(f"issues: {summary}")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
                out.write(f"### Drift\n\n{len(drifted)} of {len(rows)} units drifted. Issues: {summary}.\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write("drifted<<DRIFT_EOF\n" + "".join(u + "\n" for u in drifted) + "DRIFT_EOF\n")
            out.write(f"has_drift={'true' if drifted else 'false'}\n")
            out.write(f"failed={'true' if groups['failed'] else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
