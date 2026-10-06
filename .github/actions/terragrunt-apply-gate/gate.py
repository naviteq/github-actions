#!/usr/bin/env python3
"""After a merge, find the pull request's saved plans and open the issue that gates their apply.

`find` resolves the merged pull request and its plan artifact; `decide` checks the plan
against the merged tree and opens the approval issue, or says why it cannot. It lives
beside its action, not in scripts/, because the public mirror copies actions only.
→ docs/terragrunt-apply.md

    python3 .github/actions/terragrunt-apply-gate/gate.py find --sha "$GITHUB_SHA"
    python3 .github/actions/terragrunt-apply-gate/gate.py decide --metadata plans/metadata.json ...
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

MARKER = "terragrunt-apply"
COUNT_KEYS = ("add", "change", "replace", "destroy")


def gh(*args: str, payload: dict | None = None) -> object:
    """gh api, which follows the runner's token and pagination rules."""
    command = ["gh", "api", *args]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def merged_pull(pulls: list[dict], sha: str) -> dict | None:
    """The pull request this commit merged; None for a direct push."""
    for pull in pulls:
        if pull.get("merged_at") and pull.get("merge_commit_sha") == sha:
            return pull
    return None


def newest_artifact(artifacts: list[dict]) -> dict | None:
    live = [a for a in artifacts if not a.get("expired")]
    return max(live, key=lambda a: a.get("created_at", "")) if live else None


def approvers_of(value: str) -> list[str]:
    return [login.strip().lstrip("@") for login in value.replace("\n", ",").split(",") if login.strip()]


def check(metadata: dict, pr: int, head_sha: str, tree: str) -> str:
    """Why these plans cannot be applied to the merged commit; empty when they can."""
    if metadata.get("schema") != 1:
        return f"metadata.json schema {metadata.get('schema')!r} is not one this gate reads"
    if metadata.get("pr") != pr or metadata.get("head_sha") != head_sha:
        return (f"the plans were made for PR #{metadata.get('pr')} at {str(metadata.get('head_sha'))[:7]}, "
                f"not for PR #{pr} at {head_sha[:7]}")
    if metadata.get("tree") != tree:
        return ("the merged code differs from the code that was planned: the base branch moved between the "
                "plan and the merge. Re-plan it before applying")
    return ""


def table(units: list[dict]) -> list[str]:
    lines = ["| Unit | Profile | Add | Change | Replace | Destroy |", "|---|---|---|---|---|---|"]
    for unit in units:
        lines.append(f"| `{unit['unit']}` | {unit.get('profile', '')} | "
                     + " | ".join(str(unit.get(key, 0)) for key in COUNT_KEYS) + " |")
    return lines


def issue_body(metadata: dict, marker: dict, approvers: list[str], pr_url: str, run_url: str) -> str:
    """The approval issue: what would change, who may approve, and the exact command."""
    changing = [u for u in metadata["units"] if any(u.get(key) for key in COUNT_KEYS)]
    lines = [f"<!-- {MARKER}: {json.dumps(marker, separators=(',', ':'))} -->",
             f"### Apply the plans of [PR #{marker['pr']}]({pr_url})", "",
             f"Merged as `{marker['commit'][:7]}`; planned in [this run]({run_url}), "
             f"artifact `{metadata['artifact']}`.", "", *table(changing)]
    if metadata.get("deleted"):
        lines += ["", "**Deleted units are not applied here.** Destroy them by hand: "
                  + ", ".join(f"`{u}`" for u in metadata["deleted"])]
    if metadata.get("skipped"):
        lines += ["", "Not planned: " + ", ".join(f"`{s['unit']}` ({s['reason']})" for s in metadata["skipped"])]
    lines += ["", "The apply re-plans every unit first and stops if anything differs from the plans above.", "",
              f"To apply, one of {' '.join('@' + a for a in approvers) or '(no approvers configured)'} "
              "comments exactly:", "", "```", f"/approve apply-{marker['artifact_id']}", "```", ""]
    return "\n".join(lines)


def blocked_body(reason: str, pr: int | None, pr_url: str, commit: str) -> str:
    where = f"[PR #{pr}]({pr_url})" if pr else f"commit `{commit[:7]}`"
    return "\n".join([f"### No apply for {where}", "", reason + ".", "",
                      "Nothing was applied. Open a pull request that plans the change again; its merge "
                      "opens a new issue.", ""])


def write_outputs(**values: object) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            for key, value in values.items():
                out.write(f"{key}={value}\n")


def cmd_find(args: argparse.Namespace) -> int:
    pulls = gh(f"repos/{args.repo}/commits/{args.sha}/pulls")
    pull = merged_pull(pulls or [], args.sha)
    if pull is None:
        print(f"{args.sha[:7]} merged no pull request")
        write_outputs(pr="", head_sha="", pr_url="", artifact_id="", run_id="")
        return 0
    name = f"{args.prefix}-pr{pull['number']}-{pull['head']['sha']}"
    listing = gh(f"repos/{args.repo}/actions/artifacts?name={name}&per_page=100") or {}
    artifact = newest_artifact(listing.get("artifacts", []))
    print(f"PR #{pull['number']} at {pull['head']['sha'][:7]}: "
          + (f"artifact {artifact['id']}" if artifact else f"no live artifact named {name}"))
    write_outputs(pr=pull["number"], head_sha=pull["head"]["sha"], pr_url=pull["html_url"],
                  artifact_id=artifact["id"] if artifact else "",
                  run_id=artifact["workflow_run"]["id"] if artifact else "")
    return 0


def open_issue(args: argparse.Namespace, title: str, body: str, state: str) -> int:
    labels = [args.label, f"{args.label}:{state}"]
    issue = gh(f"repos/{args.repo}/issues", "--method", "POST",
               payload={"title": title, "body": body, "labels": labels})
    number = issue["number"]  # type: ignore[index]
    approvers = approvers_of(args.approvers)
    if state == "pending" and approvers:
        try:
            gh(f"repos/{args.repo}/issues/{number}/assignees", "--method", "POST",
               payload={"assignees": approvers})
        except subprocess.CalledProcessError:
            print("::warning title=Approvers not assigned::the issue mentions them instead")
    print(f"opened issue #{number} ({state})")
    return number


def comment_on_pr(args: argparse.Namespace, pr: int, text: str) -> None:
    gh(f"repos/{args.repo}/issues/{pr}/comments", "--method", "POST", payload={"body": text})


def cmd_decide(args: argparse.Namespace) -> int:
    pr = int(args.pr) if args.pr else None
    if pr is None:
        if args.changed == "true":
            reason = f"Commit `{args.sha[:7]}` changed `{args.working_directory}` without a pull request, so no plan exists"
            number = open_issue(args, f"No Terragrunt apply for {args.sha[:7]}: pushed without a plan",
                                blocked_body(reason, None, "", args.sha), "blocked")
            write_outputs(state="blocked", issue=number)
            return 1
        write_outputs(state="nothing", issue="")
        return 0

    metadata_path = Path(args.metadata) if args.metadata else None
    if metadata_path is None or not metadata_path.is_file():
        if args.changed != "true":
            print(f"PR #{pr} changed nothing under {args.working_directory}")
            write_outputs(state="nothing", issue="")
            return 0
        reason = (f"PR #{pr} changed `{args.working_directory}`, but no live plan artifact was found for its "
                  "last commit: it was never planned, its plan failed, or the artifact expired")
        number = open_issue(args, f"No Terragrunt apply for PR #{pr}: no plan", blocked_body(reason, pr, args.pr_url, args.sha),
                            "blocked")
        write_outputs(state="blocked", issue=number)
        return 1

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    problem = check(metadata, pr, args.head_sha, args.tree)
    if problem:
        number = open_issue(args, f"No Terragrunt apply for PR #{pr}: stale plan",
                            blocked_body(problem[0].upper() + problem[1:], pr, args.pr_url, args.sha), "blocked")
        comment_on_pr(args, pr, f"The plans of this PR cannot be applied; see #{number}.")
        write_outputs(state="blocked", issue=number)
        return 1
    if not metadata.get("has_changes"):
        comment_on_pr(args, pr, "Merged. The plan had no changes, so there is nothing to apply.")
        write_outputs(state="no-changes", issue="")
        return 0

    marker = {"artifact_id": int(args.artifact_id), "plan_run_id": metadata["plan_run_id"], "pr": pr,
              "head_sha": args.head_sha, "commit": args.sha, "tree": args.tree,
              "working_directory": args.working_directory}
    run_url = f"{args.server_url}/{args.repo}/actions/runs/{metadata['plan_run_id']}"
    body = issue_body(metadata, marker, approvers_of(args.approvers), args.pr_url, run_url)
    number = open_issue(args, f"Terragrunt apply: PR #{pr}", body, "pending")
    comment_on_pr(args, pr, f"Merged. Applying its plans waits for approval in #{number}.")
    write_outputs(state="pending", issue=number)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    find = sub.add_parser("find")
    find.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    find.add_argument("--sha", required=True)
    find.add_argument("--prefix", default="terragrunt-plan")
    decide = sub.add_parser("decide")
    decide.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    decide.add_argument("--server-url", default=os.environ.get("GITHUB_SERVER_URL", "https://github.com"))
    decide.add_argument("--sha", required=True)
    decide.add_argument("--pr", default="")
    decide.add_argument("--pr-url", default="")
    decide.add_argument("--head-sha", default="")
    decide.add_argument("--artifact-id", default="")
    decide.add_argument("--metadata", default="")
    decide.add_argument("--tree", default="")
    decide.add_argument("--changed", default="true")
    decide.add_argument("--working-directory", default=".")
    decide.add_argument("--approvers", default="")
    decide.add_argument("--label", default=MARKER)
    args = parser.parse_args(argv)
    return cmd_find(args) if args.command == "find" else cmd_decide(args)


if __name__ == "__main__":
    sys.exit(main())
