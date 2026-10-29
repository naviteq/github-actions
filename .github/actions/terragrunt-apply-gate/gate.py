#!/usr/bin/env python3
"""After a merge, find the pull request's saved plans and open the issue that gates their apply.

`find` resolves the merged pull request and its plan artifact; `decide` checks the plan
against the merged tree and opens the approval issue (or an apply-by-hand one when CI may
apply none of its units), hands a moved base on to a re-plan, or says why it cannot; `request` opens the issue that gates a requested destroy or a
re-planned apply. It lives
beside its action, not in scripts/, because the public mirror copies actions only.
→ docs/terragrunt-apply.md

    python3 .github/actions/terragrunt-apply-gate/gate.py find --sha "$GITHUB_SHA"
    python3 .github/actions/terragrunt-apply-gate/gate.py decide --metadata plans/metadata.json ...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MARKER = "terragrunt-apply"
MARKER_JSON = re.compile(r"<!-- terragrunt-apply: (\{.*?\}) -->")
COUNT_KEYS = ("add", "change", "replace", "destroy", "outputs")


def changing(unit: dict) -> bool:
    """A count, or state_changes (imports, moves, forgets), absent from older metadata. → docs/terragrunt-plan.md"""
    return any(unit.get(key) for key in COUNT_KEYS) or bool(unit.get("state_changes"))


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


def plan_runs(runs: list[dict], workflow: str) -> list[dict]:
    """The pull request's runs of the plan caller workflow, newest first."""
    mine = [r for r in runs if (r.get("path") or "").split("@")[0].endswith(f".github/workflows/{workflow}")]
    return sorted(mine, key=lambda r: r.get("created_at", ""), reverse=True)


def plan_state(runs: list[dict]) -> str:
    """running, failed or missing: why a merged pull request has no plan artifact (yet)."""
    if any(r.get("status") != "completed" for r in runs):
        return "running"
    if runs and runs[0].get("conclusion") not in ("success", None):
        return "failed"
    return "missing"


def find_artifact(repo: str, name: str, head_sha: str, workflow: str, wait_seconds: int,
                  sleep=time.sleep) -> tuple[dict | None, str]:
    """The live artifact, waiting while the plan that would upload it is still running."""
    deadline = time.monotonic() + wait_seconds
    while True:
        listing = gh(f"repos/{repo}/actions/artifacts?name={name}&per_page=100") or {}
        artifact = newest_artifact(listing.get("artifacts", []))  # type: ignore[union-attr]
        if artifact:
            return artifact, "found"
        runs = gh(f"repos/{repo}/actions/runs?head_sha={head_sha}&event=pull_request&per_page=100") or {}
        state = plan_state(plan_runs(runs.get("workflow_runs", []), workflow))  # type: ignore[union-attr]
        if state != "running" or time.monotonic() >= deadline:
            return None, state
        print(f"the plan for {head_sha[:7]} is still running; waiting for its artifact")
        sleep(30)


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


SYMBOL = {"add": "+", "change": "~", "replace": "±", "destroy": "-", "outputs": "→"}
NOUN = {"add": "to add", "change": "to change", "replace": "to replace", "destroy": "to destroy", "outputs": "outputs"}


def noun(key: str, count: int) -> str:
    if key == "outputs":
        return "output" if count == 1 else "outputs"
    return NOUN[key]


def _cell(unit: dict, key: str) -> str:
    value = int(unit.get(key) or 0)
    return f"{SYMBOL[key]}{value}" if value else "·"


def _money(value: float, currency: str = "USD", signed: bool = False) -> str:
    sign = ("+" if value > 0 else "-" if value < 0 else "±") if signed else ("-" if value < 0 else "")
    amount = f"{abs(value):,.2f}"
    return f"{sign}${amount}" if currency == "USD" else f"{sign}{amount} {currency}"


def _cost_cell(unit: dict) -> str:
    cost = unit.get("cost")
    if not isinstance(cost, dict):
        return ""
    if not cost.get("estimate"):
        return "_no estimate_"
    return _money(cost["delta"], cost.get("currency", "USD"), signed=True) if cost["delta"] else "·"


def cost_line(units: list[dict]) -> str:
    """💰 total monthly delta of the estimated units; empty when the plan made no estimate."""
    costs = [u["cost"] for u in units if isinstance(u.get("cost"), dict)]
    priced = [c for c in costs if c.get("estimate")]
    if not costs:
        return ""
    if not priced:
        return "💰 No cost estimate for these units."
    total = round(sum(c["delta"] for c in priced), 2)
    missing = len(costs) - len(priced)
    return (f"💰 **{_money(total, priced[0].get('currency', 'USD'), signed=True)}** per month"
            + (f" · no estimate for {missing} unit{'s' if missing != 1 else ''}" if missing else ""))


def table(units: list[dict]) -> list[str]:
    priced = any(isinstance(u.get("cost"), dict) for u in units)
    lines = ["| | Unit | Profile | Add | Change | Replace | Destroy | Outputs |" + (" Cost/month |" if priced else ""),
             "|:-:|---|---|:-:|:-:|:-:|:-:|:-:|" + ("--:|" if priced else "")]
    for unit in units:
        mark = "⚠️" if unit.get("destroy") or unit.get("replace") else "📝"
        lines.append(f"| {mark} | `{unit['unit']}` | {unit.get('profile', '')} | "
                     + " | ".join(_cell(unit, key) for key in COUNT_KEYS) + " |"
                     + (f" {_cost_cell(unit)} |" if priced else ""))
    return lines


def totals_line(units: list[dict]) -> str:
    sums = {key: sum(int(u.get(key) or 0) for u in units) for key in COUNT_KEYS}
    moved = sum(int(u.get("state_changes") or 0) for u in units)
    return " · ".join([f"**{SYMBOL[key]}{sums[key]}** {noun(key, sums[key])}" for key in COUNT_KEYS if sums[key]]
                      + ([f"**{moved}** to import, move or forget"] if moved else []))


ISSUE_LIMIT = 60000
SIGN = {"create": "+", "update": "!", "replace": "-/+", "delete": "-"}


def diff_lines(found: list[dict], values: bool = True) -> list[str]:
    """Each change as the plan prints it, sign first so GitHub colours it, and `!` for `~`."""
    lines: list[str] = []
    block = False
    for item in found:
        shown = item.get("lines") if values else None
        if not shown:
            lines.append(f"{SIGN[item['action']]} {item['address']}")
            block = False
            continue
        if lines and (block or shown[0][0] == "#"):
            lines.append("")
        lines += [("!" if sign == "~" else sign) + text for sign, text in shown]
        block = shown[0][0] == "#"
    return lines


def _diff_block(found: list[dict], values: bool) -> str:
    lines = diff_lines(found, values)
    fence = "````" if any("```" in line for line in lines) else "```"
    return "\n".join([f"{fence}diff", *lines, fence])


def changes_sections(units: list[dict], room: int, run_url: str) -> str:
    """Per unit, a collapsible list of its sanitised changes, cut to the room an issue body has."""
    body, cut = "", False
    note = f"_Some changes are cut to fit; the full plan is in the [run]({run_url})._\n\n"
    for unit in units:
        found = unit.get("resources") or []
        if not found:
            continue
        count = len(found)
        for values in (True, False):
            shown = "" if values or not any(item.get("lines") for item in found) else " (values cut to fit)"
            section = "\n".join([f"<details><summary><code>{unit['unit']}</code>: {count} change"
                                 f"{'s' if count != 1 else ''}{shown}</summary>", "",
                                 _diff_block(found, values), "", "</details>", "", ""])
            if len(body) + len(section) + len(note) <= room:
                body += section
                cut = cut or not values
                break
        else:
            return body + note
    return body + (note if cut else "")


def _approve_block(verb: str, artifact_id: object, approvers: list[str], two_because: str = "") -> list[str]:
    who = " ".join("@" + a for a in approvers) or "_nobody: no approvers are configured_"
    if two_because:
        return ["> [!IMPORTANT]", f"> **To {verb}, two different approvers** of {who} each comment exactly:", ">",
                "> ```", f"> /approve {verb}-{artifact_id}", "> ```", ">",
                f"> Two are needed because {two_because}. The first approval waits for the second."]
    return ["> [!IMPORTANT]", f"> **To {verb}**, one of {who} comments exactly:", ">", "> ```",
            f"> /approve {verb}-{artifact_id}", "> ```"]


def second_approver_reason(metadata: dict, threshold: float | None) -> str:
    """Why this apply needs a second approver; empty when one is enough. Unknown cost counts as over."""
    if threshold is None:
        return ""
    counted = [u for u in metadata.get("units", []) if any(u.get(k) for k in COUNT_KEYS)]
    if metadata.get("destroy") or not counted:
        return ""
    unpriced = [u["unit"] for u in counted if not (u.get("cost") or {}).get("estimate")]
    if unpriced:
        return "these units have no cost estimate: " + ", ".join(f"`{u}`" for u in unpriced)
    total = round(sum(float(u["cost"]["delta"]) for u in counted), 2)
    if total > threshold:
        currency = counted[0]["cost"].get("currency", "USD")
        return (f"it adds {_money(total, currency, signed=True)} per month, more than the "
                f"{_money(threshold, currency)} threshold")
    return ""


def _facts(*parts: str) -> str:
    return " · ".join(part for part in parts if part)


def apply_skipped(metadata: dict, lines: str) -> list[dict]:
    """The changing units an apply-mode profile skips, from the profiles action's skipped lines."""
    reasons = {}
    for line in lines.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].strip():
            reasons[parts[0].strip()] = parts[2].strip()
    return [{"unit": u["unit"], "reason": reasons[u["unit"]]} for u in metadata.get("units", [])
            if u["unit"] in reasons and changing(u)]


def only_by_hand(metadata: dict, by_hand: list[dict] | None) -> bool:
    """Every changing unit is skipped for apply, so an approval could only be refused."""
    return bool(by_hand) and len(by_hand or []) == sum(1 for u in metadata.get("units", []) if changing(u))


def _by_hand_list(skipped: list[dict]) -> str:
    return ", ".join(f"`{s['unit']}` ({s['reason']})" for s in skipped)


def issue_body(metadata: dict, marker: dict, approvers: list[str], pr_url: str, run_url: str,
               base_commit: str = "", second_approver_cost: float | None = None,
               by_hand: list[dict] | None = None) -> str:
    """The approval issue: what would change, who may approve, and the exact command."""
    shown = [u for u in metadata["units"] if changing(u)]
    destroys = sum(int(u.get("destroy") or 0) + int(u.get("replace") or 0) for u in shown)
    replanned = "source_pr" in marker
    pr = marker["source_pr"] if replanned else marker["pr"]
    planned = (f"Re-planned at `{marker['commit'][:7]}`, after the base moved under the PR's plan" if replanned
               else f"Merged as `{marker['commit'][:7]}`")
    manual = only_by_hand(metadata, by_hand)  # → docs/terragrunt-apply.md#units-ci-does-not-apply
    if manual:
        head = [f"## ✋ Apply by hand: [PR #{pr}]({pr_url})", "",
                _facts(planned, f"[Plan run]({run_url})", f"artifact `{metadata['artifact']}`"), "",
                "> [!IMPORTANT]", f"> CI does not apply these units: {_by_hand_list(by_hand or [])}.",
                f"> Apply them by hand from `{marker['commit'][:7]}`, then close this issue.", ""]
    else:
        head = [f"## 🚦 Apply waiting for approval: [PR #{pr}]({pr_url})", "",
                _facts(planned, f"[Plan run]({run_url})", f"artifact `{metadata['artifact']}`"),
                "", *_approve_block("apply", marker["artifact_id"], approvers,
                                    second_approver_reason(metadata, second_approver_cost)), ""]
    lines = [f"<!-- {MARKER}: {json.dumps(marker, separators=(',', ':'))} -->", *head]
    total = totals_line(shown)
    if total:
        lines += [total, ""]
    money_line = cost_line(shown)
    if money_line:
        lines += [money_line, ""]
    if destroys:
        lines += ["> [!WARNING]", "> This apply destroys or replaces resources. Check the units marked ⚠️.", ""]
    lines += [*table(shown), ""]
    sections = changes_sections(shown, ISSUE_LIMIT - len("\n".join(lines)) - 2000, run_url)
    if sections:
        lines += [sections]
    if metadata.get("deleted"):
        lines += ["> [!NOTE]", "> Deleted units are not destroyed by this apply: "
                  + ", ".join(f"`{u}`" for u in metadata["deleted"])
                  + f". Their destroy is requested from `{base_commit[:7] or 'the commit before this merge'}`;"
                  " a repository that chains `terragrunt-destroy` after this gate gets that issue on its own.", ""]
    if metadata.get("skipped"):
        lines += ["> [!NOTE]", "> Not planned: "
                  + ", ".join(f"`{s['unit']}` ({s['reason']})" for s in metadata["skipped"]) + ".", ""]
    if manual:
        return "\n".join(lines)
    if by_hand:
        lines += ["> [!NOTE]", f"> Not applied by CI: {_by_hand_list(by_hand)}. Apply them by hand; the issue "
                  "stays open for them after the rest is applied.", ""]
    lines += ["<details><summary>What happens on approval</summary>", "",
              "Every unit is planned again first. If any unit would now change differently from the table above, "
              "nothing is applied and the issue is refused. Otherwise the saved plans are applied, one credential "
              "profile at a time, and this issue is closed with the result.", "", "</details>", ""]
    return "\n".join(lines)


def destroy_body(metadata: dict, marker: dict, approvers: list[str], actor: str, run_url: str) -> str:
    """The destroy issue: what would go, what stays, who may approve, and the exact command."""
    going = [u for u in metadata["units"] if changing(u)]
    noun = "unit" if len(going) == 1 else "units"
    lines = [f"<!-- {MARKER}: {json.dumps(marker, separators=(',', ':'))} -->",
             f"## 🧨 Destroy waiting for approval: {len(going)} {noun}", "",
             _facts(f"Requested by @{actor}" if actor else "", f"planned at `{marker['commit'][:7]}`",
                    f"[Plan run]({run_url})", f"artifact `{metadata['artifact']}`"),
             "", *_approve_block("destroy", marker["artifact_id"], approvers), ""]
    total = totals_line(going)
    if total:
        lines += [total, ""]
    lines += ["> [!CAUTION]", "> Everything these units manage is deleted. This cannot be undone from here.", "",
              *table(going), ""]
    sections = changes_sections(going, ISSUE_LIMIT - len("\n".join(lines)) - 2000, run_url)
    if sections:
        lines += [sections]
    if metadata.get("kept"):
        lines += ["> [!NOTE]", "> Kept, as `prevent_destroy` or a dependency of one: "
                  + ", ".join(f"`{u}`" for u in metadata["kept"]) + ".", ""]
    if metadata.get("skipped"):
        lines += ["> [!NOTE]", "> Not planned: "
                  + ", ".join(f"`{s['unit']}` ({s['reason']})" for s in metadata["skipped"]) + ".", ""]
    lines += ["<details><summary>What happens on approval</summary>", "",
              "Every unit is planned for destruction again first. If anything differs from the table above, nothing "
              "is destroyed and the issue is refused. Otherwise the units are destroyed one by one, dependents first, "
              "with `destroy` so that their destroy hooks run, and this issue is closed with the result.", "",
              "</details>", ""]
    return "\n".join(lines)


def blocked_body(reason: str, pr: int | None, pr_url: str, commit: str) -> str:
    where = f"[PR #{pr}]({pr_url})" if pr else f"commit `{commit[:7]}`"
    return "\n".join([f"## ⛔ Apply blocked: {where}", "", "> [!CAUTION]", f"> {reason}.", "",
                      "Nothing was applied. **Next step:** open a pull request that plans the change again; "
                      "its merge opens a new approval issue.", ""])


def write_outputs(**values: object) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            for key, value in values.items():
                if "\n" in str(value):
                    out.write(f"{key}<<GATE_EOF\n{value}\nGATE_EOF\n")
                else:
                    out.write(f"{key}={value}\n")


def replan_outputs(metadata: dict, pr: int, sha: str) -> dict[str, str]:
    """Filters for the units the pull request planned, to plan again at the merged commit."""
    units = [u["unit"] for u in metadata.get("units", []) if not u.get("deleted")]
    if not units:
        return {"replan": "", "replan_ref": "", "replan_pr": ""}
    return {"replan": "\n".join(f"./{unit}" for unit in units), "replan_ref": sha, "replan_pr": str(pr)}


def deleted_outputs(metadata: dict, base_commit: str) -> dict[str, str]:
    """Filters for the units the merge deleted, and the commit where they still exist."""
    deleted = metadata.get("deleted") or []
    if not deleted or not base_commit:
        return {"deleted": "", "deleted_ref": ""}
    return {"deleted": "\n".join(f"./{unit}" for unit in deleted), "deleted_ref": base_commit}


def cmd_find(args: argparse.Namespace) -> int:
    pulls = gh(f"repos/{args.repo}/commits/{args.sha}/pulls")
    pull = merged_pull(pulls or [], args.sha)
    if pull is None:
        print(f"{args.sha[:7]} merged no pull request")
        write_outputs(pr="", head_sha="", pr_url="", artifact_id="", run_id="")
        return 0
    name = f"{args.prefix}-pr{pull['number']}-{pull['head']['sha']}"
    artifact, state = find_artifact(args.repo, name, pull["head"]["sha"], args.plan_workflow, args.wait_seconds)
    print(f"PR #{pull['number']} at {pull['head']['sha'][:7]}: "
          + (f"artifact {artifact['id']}" if artifact else f"no live artifact named {name} (plan {state})"))
    write_outputs(pr=pull["number"], head_sha=pull["head"]["sha"], pr_url=pull["html_url"],
                  artifact_id=artifact["id"] if artifact else "",
                  run_id=artifact["workflow_run"]["id"] if artifact else "", plan_state=state)
    return 0


def open_issue(args: argparse.Namespace, title: str, body: str, state: str, extra: tuple[str, ...] = ()) -> int:
    labels = [args.label, f"{args.label}:{state}", *(f"{args.label}:{label}" for label in extra)]
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
        why = {
            "failed": "its plan failed",
            "running": "its plan was still running when the wait ran out",
        }.get(args.plan_state, "it was never planned, or the artifact expired")
        reason = (f"PR #{pr} changed `{args.working_directory}`, but no live plan artifact was found for its "
                  f"last commit: {why}")
        number = open_issue(args, f"No Terragrunt apply for PR #{pr}: no plan", blocked_body(reason, pr, args.pr_url, args.sha),
                            "blocked")
        write_outputs(state="blocked", issue=number)
        return 1

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    problem = check(metadata, pr, args.head_sha, args.tree)
    # A moved base makes the saved plans stale, not the deletions: the destroy plans afresh
    # from the commit before the merge and asks for its own approval. → docs/terragrunt-destroy.md
    moved_base = bool(problem) and check(metadata, pr, args.head_sha, metadata.get("tree", "")) == ""
    if not problem or moved_base:
        write_outputs(**deleted_outputs(metadata, args.base_commit))
    # Only the base moved: the same pull request plans again at the merged commit. → docs/terragrunt-apply.md
    if moved_base and args.replan == "true":
        outputs = replan_outputs(metadata, pr, args.sha)
        if outputs["replan"]:
            comment_on_pr(args, pr, f"🔄 Merged after another change under `{args.working_directory}`, so its plans "
                          f"are stale. Planning its units again at `{args.sha[:7]}`; the approval issue follows.")
        write_outputs(**outputs, state="replan" if outputs["replan"] else "nothing", issue="")
        return 0
    if problem:
        number = open_issue(args, f"No Terragrunt apply for PR #{pr}: stale plan",
                            blocked_body(problem[0].upper() + problem[1:], pr, args.pr_url, args.sha), "blocked")
        comment_on_pr(args, pr, f"⛔ The plans of this pull request cannot be applied; see #{number}.")
        write_outputs(state="blocked", issue=number)
        return 1
    if not any(changing(u) for u in metadata["units"]):
        why = "only deletes units, whose destroy is a separate request" if metadata.get("deleted") else "had no changes"
        comment_on_pr(args, pr, f"✅ Merged. The plan {why}, so there is nothing to apply.")
        write_outputs(state="no-changes", issue="")
        return 0

    marker = {"artifact_id": int(args.artifact_id), "plan_run_id": metadata["plan_run_id"], "pr": pr,
              "head_sha": args.head_sha, "commit": args.sha, "tree": args.tree,
              "working_directory": args.working_directory}
    run_url = f"{args.server_url}/{args.repo}/actions/runs/{metadata['plan_run_id']}"
    threshold = float(args.second_approver_cost) if args.second_approver_cost.strip() else None
    by_hand = apply_skipped(metadata, args.apply_skipped)
    body = issue_body(metadata, marker, approvers_of(args.approvers), args.pr_url, run_url, args.base_commit, threshold,
                      by_hand)
    if only_by_hand(metadata, by_hand):
        number = open_issue(args, f"Terragrunt apply by hand: PR #{pr}", body, "by-hand")
        comment_on_pr(args, pr, f"✋ Merged. CI does not apply its units; apply them by hand, see #{number}.")
        write_outputs(state="by-hand", issue=number)
        return 0
    two = ("two-approvers",) if second_approver_reason(metadata, threshold) else ()
    number = open_issue(args, f"Terragrunt apply: PR #{pr}", body, "pending", two)
    comment_on_pr(args, pr, f"🚦 Merged. Applying its plans waits for approval in #{number}.")
    write_outputs(state="pending", issue=number)
    return 0


def superseded(issues: list[dict], plan_run_id: object, artifact_id: object) -> list[int]:
    """Open apply issues of an earlier attempt of this plan run: a re-run deletes their artifact."""
    found = []
    for issue in issues:
        match = MARKER_JSON.search(issue.get("body") or "")
        marker = json.loads(match.group(1)) if match else {}
        if marker.get("action", "apply") == "apply" and marker.get("plan_run_id") == plan_run_id \
                and str(marker.get("artifact_id")) != str(artifact_id) and not issue.get("pull_request"):
            found.append(issue["number"])
    return sorted(found)


def close_superseded(args: argparse.Namespace, metadata: dict, text: str) -> None:
    """Close what an earlier attempt of this re-plan opened, so nobody approves a deleted artifact. → docs/terragrunt-replan.md"""
    issues = gh(f"repos/{args.repo}/issues?labels={args.label}&state=open&per_page=100") or []
    for number in superseded(issues, metadata["plan_run_id"], args.artifact_id):  # type: ignore[arg-type]
        try:
            gh(f"repos/{args.repo}/issues/{number}/labels/{args.label}:pending", "--method", "DELETE")
        except subprocess.CalledProcessError:
            pass
        gh(f"repos/{args.repo}/issues/{number}/labels", "--method", "POST",
           payload={"labels": [f"{args.label}:superseded"]})
        gh(f"repos/{args.repo}/issues/{number}/comments", "--method", "POST", payload={"body": text})
        gh(f"repos/{args.repo}/issues/{number}", "--method", "PATCH",
           payload={"state": "closed", "state_reason": "not_planned"})
        print(f"closed issue #{number}, superseded")


def request_apply(args: argparse.Namespace, metadata: dict) -> int:
    """The approval issue for plans made again after the merge, since the base moved under the PR's."""
    pr = int(args.pr) if args.pr else None
    rerun = "its plan run was run again, which replaced this issue's plan artifact"
    if not metadata.get("has_changes"):
        print("Nothing to apply: the re-plan changes nothing.")
        close_superseded(args, metadata, f"⏭️ **Superseded:** {rerun}, and the new plan changes nothing.")
        if pr:
            comment_on_pr(args, pr, "✅ Planned again after the merge: nothing to apply.")
        write_outputs(state="no-changes", issue="")
        return 0
    marker = {"artifact_id": int(args.artifact_id), "plan_run_id": metadata["plan_run_id"], "pr": metadata.get("pr"),
              "head_sha": metadata["head_sha"], "commit": metadata["commit"], "tree": metadata["tree"],
              "working_directory": metadata["working_directory"], "source_pr": pr}
    run_url = f"{args.server_url}/{args.repo}/actions/runs/{metadata['plan_run_id']}"
    pr_url = args.pr_url or (f"{args.server_url}/{args.repo}/pull/{pr}" if pr else run_url)
    threshold = float(args.second_approver_cost) if args.second_approver_cost.strip() else None
    by_hand = apply_skipped(metadata, args.apply_skipped)
    body = issue_body(metadata, marker, approvers_of(args.approvers), pr_url, run_url, "", threshold, by_hand)
    title = f"PR #{pr} (re-planned)" if pr else f"{metadata['commit'][:7]} (re-planned)"
    if only_by_hand(metadata, by_hand):
        number = open_issue(args, f"Terragrunt apply by hand: {title}", body, "by-hand")
        close_superseded(args, metadata, f"⏭️ **Superseded by #{number}:** {rerun}.")
        if pr:
            comment_on_pr(args, pr, f"✋ Planned again after the merge. CI does not apply its units; apply them by "
                          f"hand, see #{number}.")
        write_outputs(state="by-hand", issue=number)
        return 0
    two = ("two-approvers",) if second_approver_reason(metadata, threshold) else ()
    number = open_issue(args, f"Terragrunt apply: {title}", body, "pending", two)
    close_superseded(args, metadata, f"⏭️ **Superseded by #{number}:** {rerun}.")
    if pr:
        comment_on_pr(args, pr, f"🚦 Planned again after the merge. Applying waits for approval in #{number}.")
    write_outputs(state="pending", issue=number)
    return 0


def cmd_request(args: argparse.Namespace) -> int:
    metadata = json.loads(Path(args.metadata).read_text(encoding="utf-8"))
    if metadata.get("schema") != 1 or bool(metadata.get("destroy")) != (args.action == "destroy"):
        print(f"::error title=Not {args.action} plans::the artifact's metadata.json does not describe {args.action} plans")
        write_outputs(state="blocked", issue="")
        return 1
    if args.action == "apply":
        return request_apply(args, metadata)
    if not metadata.get("has_changes"):
        print("Nothing to destroy: every selected unit is already empty or kept.")
        write_outputs(state="no-changes", issue="")
        return 0
    marker = {"action": "destroy", "artifact_id": int(args.artifact_id), "plan_run_id": metadata["plan_run_id"],
              "pr": metadata.get("pr"), "head_sha": metadata["head_sha"], "commit": metadata["commit"],
              "tree": metadata["tree"], "working_directory": metadata["working_directory"]}
    run_url = f"{args.server_url}/{args.repo}/actions/runs/{metadata['plan_run_id']}"
    body = destroy_body(metadata, marker, approvers_of(args.approvers), args.actor, run_url)
    going = [u["unit"] for u in metadata["units"] if changing(u)]
    title = ", ".join(going[:3]) + (f" and {len(going) - 3} more" if len(going) > 3 else "")
    number = open_issue(args, f"Terragrunt destroy: {title}", body, "pending")
    write_outputs(state="pending", issue=number)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    find = sub.add_parser("find")
    find.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    find.add_argument("--sha", required=True)
    find.add_argument("--prefix", default="terragrunt-plan")
    find.add_argument("--plan-workflow", default="terragrunt-plan.yaml")
    find.add_argument("--wait-seconds", type=int, default=1800)
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
    decide.add_argument("--plan-state", default="")
    decide.add_argument("--second-approver-cost", default="", help="Monthly cost delta above which two approve")
    decide.add_argument("--base-commit", default="", help="The merge's first parent, where deleted units still exist")
    decide.add_argument("--replan", default="false", help="true hands a moved base on to a re-plan instead of blocking")
    decide.add_argument("--apply-skipped", default="", help="unit<TAB>profile<TAB>reason lines an apply profile skips")
    request = sub.add_parser("request")
    request.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    request.add_argument("--server-url", default=os.environ.get("GITHUB_SERVER_URL", "https://github.com"))
    request.add_argument("--actor", default=os.environ.get("GITHUB_ACTOR", ""))
    request.add_argument("--artifact-id", required=True)
    request.add_argument("--metadata", required=True)
    request.add_argument("--approvers", default="")
    request.add_argument("--label", default="terragrunt-destroy")
    request.add_argument("--action", choices=("destroy", "apply"), default="destroy")
    request.add_argument("--pr", default="", help="For a re-planned apply, the pull request it belongs to")
    request.add_argument("--pr-url", default="")
    request.add_argument("--second-approver-cost", default="", help="Monthly cost delta above which two approve")
    request.add_argument("--apply-skipped", default="", help="For an apply, the lines an apply profile skips")
    args = parser.parse_args(argv)
    return {"find": cmd_find, "decide": cmd_decide, "request": cmd_request}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
