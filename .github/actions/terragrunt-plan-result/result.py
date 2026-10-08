#!/usr/bin/env python3
"""Merge the plan jobs' counts, hand the plans over to the apply, and render the PR comment and Slack summary.

Every file this writes carries resource addresses and actions at most, never a value.
It lives beside its action, not in scripts/, because the public mirror copies actions
only. → docs/terragrunt-plan.md

    python3 .github/actions/terragrunt-plan-result/result.py --counts-dir counts \\
        --plans-dir plans --units "$UNITS" --context-json "$CONTEXT" --comment-file comment.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

COMMENT_LIMIT = 60000
SLACK_UNITS = 20
COUNT_KEYS = ("add", "change", "replace", "destroy", "outputs")
SIGN = {"create": "+", "update": "~", "replace": "-/+", "delete": "-"}


def merge_counts(counts_dir: Path, units: list[str]) -> list[dict]:
    """Every job's rows in one list, in the order the units were selected."""
    rows: list[dict] = []
    for path in sorted(counts_dir.glob("*.counts.json")):
        rows.extend(json.loads(path.read_text(encoding="utf-8")))
    position = {unit: index for index, unit in enumerate(units)}
    return sorted(rows, key=lambda row: position.get(row["unit"], len(position)))


def merge_resources(counts_dir: Path) -> dict[str, list[dict[str, str]]]:
    found: dict[str, list[dict[str, str]]] = {}
    for path in sorted(counts_dir.glob("*.resources.json")):
        found.update(json.loads(path.read_text(encoding="utf-8")))
    return found


def has_changes(rows: list[dict]) -> bool:
    return any(row.get(key) for row in rows for key in COUNT_KEYS)


def join_manifests(plans_dir: Path) -> list[list[str]]:
    """manifest.<profile>.tsv files into one manifest.tsv; returns its rows."""
    rows: list[list[str]] = []
    parts = sorted(plans_dir.glob("manifest.*.tsv"))
    for part in parts:
        rows.extend(line.split("\t") for line in part.read_text(encoding="utf-8").splitlines() if line)
        part.unlink()
    if rows:
        (plans_dir / "manifest.tsv").write_text("".join("\t".join(r) + "\n" for r in rows), encoding="utf-8")
    return rows


def metadata(context: dict, rows: list[dict], manifest: list[list[str]], skipped: list[list[str]]) -> dict:
    """What the apply checks a plan against before it touches anything."""
    files = {row[0]: row[1] for row in manifest if len(row) >= 2}
    units = [
        {"unit": row["unit"], "profile": row.get("profile", ""), "file": files.get(row["unit"], ""),
         "changeset": row.get("changeset", ""), **{key: row.get(key, 0) for key in COUNT_KEYS}}
        for row in rows if not row.get("deleted") and not row.get("error") and not row.get("kept")
    ]
    return {
        "schema": 1,
        **context,
        "has_changes": has_changes(rows),
        "units": units,
        "deleted": [row["unit"] for row in rows if row.get("deleted")],
        "kept": [row["unit"] for row in rows if row.get("kept")],
        "skipped": [{"unit": s[0], "profile": s[1], "reason": s[2]} for s in skipped if len(s) >= 3],
    }


SYMBOL = {"add": "+", "change": "~", "replace": "±", "destroy": "-", "outputs": "→"}
NOUN = {"add": "to add", "change": "to change", "replace": "to replace", "destroy": "to destroy", "outputs": "outputs"}


def noun(key: str, count: int) -> str:
    if key == "outputs":
        return "output" if count == 1 else "outputs"
    return NOUN[key]


def changes(row: dict) -> bool:
    return any(row.get(key) for key in COUNT_KEYS)


def totals(rows: list[dict]) -> dict[str, int]:
    return {key: sum(int(row.get(key) or 0) for row in rows if not row.get("error")) for key in COUNT_KEYS}


def quiet(rows: list[dict]) -> bool:
    """Nothing worth a comment or a message: no unit changes and none failed."""
    return not any(changes(row) or row.get("error") for row in rows)


def units(count: int) -> str:
    return f"{count} unit" if count == 1 else f"{count} units"


def status(rows: list[dict]) -> tuple[str, str]:
    """Emoji and one-line outcome for a heading."""
    failed = [row for row in rows if row.get("error")]
    changing = [row for row in rows if changes(row)]
    sums = totals(rows)
    if not rows:
        return "✅", "nothing to plan"
    if failed:
        return "❌", f"{len(failed)} of {units(len(rows))} failed to plan"
    if not changing:
        return "✅", f"no changes in {units(len(rows))}"
    verb = "changes" if len(changing) == 1 else "change"
    emoji = "⚠️" if sums["destroy"] or sums["replace"] else "📝"
    return emoji, f"{len(changing)} of {units(len(rows))} {verb}"


def totals_line(rows: list[dict]) -> str:
    sums = totals(rows)
    parts = [f"**{SYMBOL[key]}{sums[key]}** {noun(key, sums[key])}" for key in COUNT_KEYS if sums[key]]
    return " · ".join(parts)


def _resources(count: int) -> str:
    return "1 resource" if count == 1 else f"{count} resources"


def _cell(row: dict, key: str) -> str:
    value = int(row.get(key) or 0)
    return f"{SYMBOL[key]}{value}" if value else "·"


def _unit_status(row: dict) -> str:
    if row.get("kept"):
        return "🛡️"
    if row.get("error"):
        return "❌"
    if row.get("deleted"):
        return "🗑️"
    if row.get("destroy") or row.get("replace"):
        return "⚠️"
    return "📝" if changes(row) else "✅"


def table(rows: list[dict]) -> list[str]:
    lines = ["| | Unit | Profile | Add | Change | Replace | Destroy | Outputs |",
             "|:-:|---|---|:-:|:-:|:-:|:-:|:-:|"]
    for row in rows:
        unit = f"`{row['unit']}`" + (" _(deleted)_" if row.get("deleted") else "")
        if row.get("kept"):
            cells = "kept: `prevent_destroy` | | | |"
        elif row.get("error"):
            cells = f"**{row['error']}** | | | |"
        else:
            cells = " | ".join(_cell(row, key) for key in COUNT_KEYS)
        lines.append(f"| {_unit_status(row)} | {unit} | {row.get('profile', '')} | {cells} |")
    return lines


def comment(rows: list[dict], resources: dict[str, list[dict[str, str]]], skipped: list[list[str]],
            run_url: str, context: dict | None = None, limit: int = COMMENT_LIMIT) -> str:
    """The sticky PR comment: the outcome at a glance, the table, then each changing unit's addresses."""
    context = context or {}
    emoji, outcome = status(rows)
    head = [f"### {emoji} Terragrunt plan: {outcome}", ""]
    line = totals_line(rows)
    if line:
        head += [line, ""]
    failed = [row for row in rows if row.get("error")]
    if failed:
        head += ["> [!CAUTION]", "> These units failed to plan, so their changes are unknown: "
                 + ", ".join(f"`{row['unit']}`" for row in failed) + f". The [run]({run_url}) says why.", ""]
    sums = totals(rows)
    if sums["destroy"] or sums["replace"]:
        what = " and ".join(part for part in (
            f"destroys {_resources(sums['destroy'])}" if sums["destroy"] else "",
            f"replaces {_resources(sums['replace'])}" if sums["replace"] else "") if part)
        head += ["> [!WARNING]", f"> This plan {what}. Check the units marked ⚠️ before merging.", ""]
    if rows:
        head += [*table(rows), ""]
    deleted = [row["unit"] for row in rows if row.get("deleted")]
    if deleted:
        head += ["> [!NOTE]", "> Deleted units are never applied. After the merge their destroy is a separate "
                 "request with its own approval: " + ", ".join(f"`{u}`" for u in deleted) + ".", ""]
    if skipped:
        head += ["> [!NOTE]", "> Not planned: " + ", ".join(f"`{s[0]}` ({s[2]})" for s in skipped if len(s) >= 3)
                 + ".", ""]
    commit = str(context.get("commit") or "")[:7]
    footer = "<sub>" + " · ".join(part for part in (
        f"Planned at `{commit}`" if commit else "", f"[Run]({run_url})" if run_url else "",
        "Updated on every push") if part) + "</sub>\n"
    body = "\n".join(head) + "\n"

    sections = []
    for row in rows:
        # A deleted unit's destroy plan is counted from the log; it has no addresses.
        found = resources.get(row["unit"]) if changes(row) else None
        if not found:
            continue
        count = len(found)
        lines = [f"<details><summary>{_unit_status(row)} <code>{row['unit']}</code>: "
                 f"{count} change{'s' if count != 1 else ''}</summary>", "", "```diff"]
        lines += [f"{SIGN[r['action']]} {r['address']}" for r in found]
        lines += ["```", "", "</details>", "", ""]
        sections.append("\n".join(lines))
    note = f"_Resource lists cut to fit; the full plan is in the [run]({run_url})._\n\n"
    for section in sections:
        if len(body) + len(section) + len(note) + len(footer) > limit:
            return body + note + footer
        body += section
    return body + footer


SLACK_COLOR = {"✅": "#2eb886", "📝": "#d4a72c", "⚠️": "#e8912d", "❌": "#e01e5a"}


def slack_payload(rows: list[dict], context: dict, run_url: str) -> dict:
    """Counts per unit and links only: a Slack channel is read by more people than the repository."""
    emoji, outcome = status(rows)
    repository = context.get("repository", "")
    pr = context.get("pr")
    commit = str(context.get("commit") or "")[:7]
    repo_url = run_url.split("/actions/")[0] if "/actions/" in run_url else ""
    where = f"<{repo_url}/pull/{pr}|PR #{pr}>" if pr and repo_url else (f"PR #{pr}" if pr else f"`{commit}`")
    head = f"{emoji} Terragrunt plan: {outcome}"
    fields = [{"type": "mrkdwn", "text": f"*Repository*\n{repository}"},
              {"type": "mrkdwn", "text": f"*Change*\n{where}"}]
    sums = totals(rows)
    if any(sums.values()):
        fields.append({"type": "mrkdwn", "text": "*Totals*\n" + "  ".join(
            f"{SYMBOL[key]}{sums[key]} {noun(key, sums[key])}" for key in COUNT_KEYS if sums[key])})
    listed = [row for row in rows if changes(row) or row.get("error")]
    lines = []
    for row in listed[:SLACK_UNITS]:
        unit = f"`{row['unit']}`" + (" (deleted)" if row.get("deleted") else "")
        if row.get("error"):
            lines.append(f"{_unit_status(row)} {unit}: {row['error']}")
        else:
            counts = "  ".join(f"{SYMBOL[key]}{row.get(key, 0)}" for key in COUNT_KEYS if row.get(key))
            lines.append(f"{_unit_status(row)} {unit}  {counts}")
    if len(listed) > SLACK_UNITS:
        lines.append(f"…and {len(listed) - SLACK_UNITS} more")
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": head[:150], "emoji": True}},
                          {"type": "section", "fields": fields}]
    if lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}})
    links = [f"<{run_url}|View the run>"] if run_url else []
    if pr and repo_url:
        links.insert(0, f"<{repo_url}/pull/{pr}|Open the pull request>")
    if commit:
        links.append(f"commit `{commit}`")
    if links:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(links)}]})
    return {"text": f"{head} ({repository})",
            "attachments": [{"color": SLACK_COLOR[emoji], "blocks": blocks}]}


def _lines(value: str) -> list[str]:
    return [line for line in value.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--counts-dir", required=True)
    parser.add_argument("--plans-dir", default="", help="Downloaded encrypted plans, if any; gets manifest.tsv and metadata.json")
    parser.add_argument("--units", default="")
    parser.add_argument("--skipped", default="")
    parser.add_argument("--context-json", default="{}", help="Run facts to put in metadata.json")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--comment-file", default="")
    parser.add_argument("--slack-file", default="", help="Write the Slack message payload here, as JSON")
    args = parser.parse_args(argv)

    counts_dir = Path(args.counts_dir)
    rows = merge_counts(counts_dir, _lines(args.units)) if counts_dir.is_dir() else []
    resources = merge_resources(counts_dir) if counts_dir.is_dir() else {}
    skipped = [line.split("\t") for line in _lines(args.skipped)]
    handed_over = False
    # Also with nothing planned: the gate must tell "nothing to apply" from "never planned".
    if args.plans_dir:
        Path(args.plans_dir).mkdir(parents=True, exist_ok=True)
        manifest = join_manifests(Path(args.plans_dir))
        data = metadata(json.loads(args.context_json), rows, manifest, skipped)
        (Path(args.plans_dir) / "metadata.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        handed_over = True
    if args.comment_file:
        context = json.loads(args.context_json)
        Path(args.comment_file).write_text(comment(rows, resources, skipped, args.run_url, context), encoding="utf-8")
    if args.slack_file:
        payload = slack_payload(rows, json.loads(args.context_json), args.run_url)
        Path(args.slack_file).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"changes={json.dumps(rows, separators=(',', ':'))}\n")
            out.write(f"has_changes={'true' if has_changes(rows) else 'false'}\n")
            out.write(f"handed_over={'true' if handed_over else 'false'}\n")
            out.write(f"quiet={'true' if quiet(rows) else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
