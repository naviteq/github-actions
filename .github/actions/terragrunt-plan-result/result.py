#!/usr/bin/env python3
"""Merge the plan jobs' counts, hand the plans over to the apply, and render the PR comment and Slack summary.

Attribute values reach the comment and metadata.json only as the report sanitised them;
the Slack summary carries counts alone.
It lives beside its action, not in scripts/, because the public mirror copies actions
only. → docs/terragrunt-plan.md

    python3 .github/actions/terragrunt-plan-result/result.py --counts-dir counts \\
        --plans-dir plans --units "$UNITS" --context-json "$CONTEXT" --comment-file comment.md

A fork's run bundles the same inputs with --report-out; the base repository renders them
with --report-in, reading the bundle as data only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

COMMENT_LIMIT = 60000
SLACK_UNITS = 20
COUNT_KEYS = ("add", "change", "replace", "destroy", "outputs")
SIGN = {"create": "+", "update": "!", "replace": "-/+", "delete": "-"}
REPORT_FILES = ("*.counts.json", "*.resources.json", "*.costs.json")
# Only these facts come from a fork's bundle; who and where are the base repository's.
REPORT_CONTEXT = ("commit", "working_directory")


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


def merge_counts(counts_dir: Path, units: list[str]) -> list[dict]:
    """Every job's rows in one list, in the order the units were selected."""
    rows: list[dict] = []
    for path in sorted(counts_dir.glob("*.counts.json")):
        rows.extend(json.loads(path.read_text(encoding="utf-8")))
    position = {unit: index for index, unit in enumerate(units)}
    return sorted(rows, key=lambda row: position.get(row["unit"], len(position)))


def merge_costs(counts_dir: Path) -> dict[str, dict]:
    """Each unit's Infracost estimate, when the plan made one."""
    found: dict[str, dict] = {}
    for path in sorted(counts_dir.glob("*.costs.json")):
        found.update(json.loads(path.read_text(encoding="utf-8")))
    return found


def money(value: float, currency: str = "USD", signed: bool = False) -> str:
    sign = ("+" if value > 0 else "-" if value < 0 else "±") if signed else ("-" if value < 0 else "")
    amount = f"{abs(value):,.2f}"
    return f"{sign}${amount}" if currency == "USD" else f"{sign}{amount} {currency}"


def cost_total(rows: list[dict]) -> tuple[float, str, int, int] | None:
    """Total monthly delta, currency, units with and without an estimate; None when nothing was estimated."""
    costs = [row["cost"] for row in rows if isinstance(row.get("cost"), dict)]
    priced = [cost for cost in costs if cost.get("estimate")]
    if not costs:
        return None
    currency = priced[0].get("currency", "USD") if priced else "USD"
    return round(sum(cost["delta"] for cost in priced), 2), currency, len(priced), len(costs) - len(priced)


def cost_cell(row: dict) -> str:
    cost = row.get("cost")
    if not isinstance(cost, dict):
        return ""
    if not cost.get("estimate"):
        return "_no estimate_"
    return money(cost["delta"], cost.get("currency", "USD"), signed=True) if cost["delta"] else "·"


def cost_section(rows: list[dict]) -> list[str]:
    """Before → after per estimated unit, and the resources that move the most."""
    lines = []
    for row in rows:
        cost = row.get("cost")
        if not isinstance(cost, dict) or not cost.get("estimate") or not cost.get("delta"):
            continue
        currency = cost.get("currency", "USD")
        lines.append(f"- `{row['unit']}`: {money(cost['before'], currency)} → {money(cost['after'], currency)} "
                     f"per month (**{money(cost['delta'], currency, signed=True)}**)")
        lines += [f"  - `{m['address']}` {money(m['delta'], currency, signed=True)}" for m in cost.get("top") or []]
    if not lines:
        return []
    return ["<details><summary>💰 Where the cost changes</summary>", "", *lines, "",
            "Estimated by Infracost from list prices; usage-based costs are not included.", "", "</details>", ""]


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


def metadata(context: dict, rows: list[dict], manifest: list[list[str]], skipped: list[list[str]],
             resources: dict[str, list[dict]] | None = None) -> dict:
    """What the apply checks a plan against before it touches anything, and what the gate shows."""
    files = {row[0]: row[1] for row in manifest if len(row) >= 2}
    resources = resources or {}
    units = [
        {"unit": row["unit"], "profile": row.get("profile", ""), "file": files.get(row["unit"], ""),
         "changeset": row.get("changeset", ""), **{key: row.get(key, 0) for key in COUNT_KEYS},
         "resources": resources.get(row["unit"], []), **({"cost": row["cost"]} if "cost" in row else {})}
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


def quiet(rows: list[dict], skipped: list[list[str]] | None = None) -> bool:
    """Nothing worth a comment or a message: no unit changes, none failed and none was left unplanned."""
    return not skipped and not any(changes(row) or row.get("error") for row in rows)


def units(count: int) -> str:
    return f"{count} unit" if count == 1 else f"{count} units"


def status(rows: list[dict], skipped: list[list[str]] | None = None) -> tuple[str, str]:
    """Emoji and one-line outcome for a heading."""
    emoji, outcome = _status(rows)
    if not skipped:
        return emoji, outcome
    unplanned = f"{units(len(skipped))} not planned"
    return ("⏭️", unplanned) if not rows else (emoji, f"{outcome} · {unplanned}")


def _status(rows: list[dict]) -> tuple[str, str]:
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
    priced = cost_total(rows) is not None
    lines = ["| | Unit | Profile | Add | Change | Replace | Destroy | Outputs |" + (" Cost/month |" if priced else ""),
             "|:-:|---|---|:-:|:-:|:-:|:-:|:-:|" + ("--:|" if priced else "")]
    for row in rows:
        unit = f"`{row['unit']}`" + (" _(deleted)_" if row.get("deleted") else "")
        if row.get("kept"):
            cells = "kept: `prevent_destroy` | | | |"
        elif row.get("error"):
            cells = f"**{row['error']}** | | | |"
        else:
            cells = " | ".join(_cell(row, key) for key in COUNT_KEYS)
        lines.append(f"| {_unit_status(row)} | {unit} | {row.get('profile', '')} | {cells} |"
                     + (f" {cost_cell(row)} |" if priced else ""))
    return lines


def comment(rows: list[dict], resources: dict[str, list[dict[str, str]]], skipped: list[list[str]],
            run_url: str, context: dict | None = None, limit: int = COMMENT_LIMIT) -> str:
    """The sticky PR comment: the outcome at a glance, the table, then each changing unit's addresses."""
    context = context or {}
    emoji, outcome = status(rows, skipped)
    head = [f"### {emoji} Terragrunt plan: {outcome}", ""]
    line = totals_line(rows)
    if line:
        head += [line, ""]
    total = cost_total(rows)
    if total and (total[2] or total[3]):
        delta, currency, priced, unpriced = total
        text = f"💰 **{money(delta, currency, signed=True)}** per month" if priced else "💰 No cost estimate"
        head += [text + (f" · no estimate for {units(unpriced)}" if unpriced and priced else "") + "", ""]
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
    head += cost_section(rows)
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

    note = f"_Some changes are cut to fit; the full plan is in the [run]({run_url})._\n\n"
    cut = False
    for row in rows:
        # A deleted unit's destroy plan is counted from the log; it has no addresses.
        found = resources.get(row["unit"]) if changes(row) else None
        if not found:
            continue
        full = section(row, found, values=True)
        brief = section(row, found, values=False)
        room = limit - len(body) - len(note) - len(footer)
        if len(full) <= room:
            body += full
        elif len(brief) <= room:
            body, cut = body + brief, True
        else:
            return body + note + footer
    return body + (note if cut else "") + footer


def diff_block(found: list[dict], values: bool = True) -> str:
    """Each change as the plan prints it or, with values left out, its address only."""
    lines = diff_lines(found, values)
    fence = "````" if any("```" in line for line in lines) else "```"
    return "\n".join([f"{fence}diff", *lines, fence])


def section(row: dict, found: list[dict], values: bool = True) -> str:
    count = len(found)
    shown = "" if values or not any(item.get("lines") for item in found) else " (values cut to fit)"
    return "\n".join([f"<details><summary>{_unit_status(row)} <code>{row['unit']}</code>: "
                      f"{count} change{'s' if count != 1 else ''}{shown}</summary>", "",
                      diff_block(found, values), "", "</details>", "", ""])


SLACK_COLOR = {"✅": "#2eb886", "📝": "#d4a72c", "⚠️": "#e8912d", "❌": "#e01e5a", "⏭️": "#868e96"}


def slack_payload(rows: list[dict], context: dict, run_url: str, skipped: list[list[str]] | None = None) -> dict:
    """Counts per unit and links only: a Slack channel is read by more people than the repository."""
    emoji, outcome = status(rows, skipped)
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
    total = cost_total(rows)
    if total and total[2]:
        fields.append({"type": "mrkdwn", "text": f"*Monthly cost*\n{money(total[0], total[1], signed=True)}"})
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
    for unit in (skipped or [])[:SLACK_UNITS]:
        lines.append(f"⏭️ `{unit[0]}`: not planned")
    if len(skipped or []) > SLACK_UNITS:
        lines.append(f"…and {len(skipped or []) - SLACK_UNITS} more not planned")
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


def write_report(report_dir: Path, counts_dir: Path, units: list[str], skipped: list[list[str]],
                 context: dict) -> None:
    """What the comment is rendered from, for the base repository to render a fork's comment."""
    target = report_dir / "counts"
    target.mkdir(parents=True, exist_ok=True)
    for pattern in REPORT_FILES:
        for path in sorted(counts_dir.glob(pattern)) if counts_dir.is_dir() else []:
            shutil.copyfile(path, target / path.name)
    data = {"schema": 1, "units": units, "skipped": skipped,
            "context": {key: context[key] for key in REPORT_CONTEXT if key in context}}
    (report_dir / "report.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def read_report(report_dir: Path) -> tuple[Path, list[str], list[list[str]], dict]:
    """A fork's bundle, kept to the shapes write_report makes."""
    data = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise SystemExit(f"{report_dir}/report.json is not a schema 1 plan report")
    units = [str(unit) for unit in data.get("units") or []]
    skipped = [[str(cell) for cell in row] for row in data.get("skipped") or [] if isinstance(row, list)]
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    return report_dir / "counts", units, skipped, {key: str(context[key]) for key in REPORT_CONTEXT if key in context}


def safe_path(value: str) -> str:
    """A directory name fit for a one-line output and an HTML comment marker."""
    return re.sub(r"[^A-Za-z0-9._/-]", "", value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--counts-dir", default="")
    parser.add_argument("--plans-dir", default="", help="Downloaded encrypted plans, if any; gets manifest.tsv and metadata.json")
    parser.add_argument("--units", default="")
    parser.add_argument("--skipped", default="")
    parser.add_argument("--context-json", default="{}", help="Run facts to put in metadata.json")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--comment-file", default="")
    parser.add_argument("--slack-file", default="", help="Write the Slack message payload here, as JSON")
    parser.add_argument("--report-out", default="", help="Bundle the comment's inputs here, for a fork's pull request")
    parser.add_argument("--report-in", default="", help="Render a bundle a fork's run made; --context-json wins over it")
    args = parser.parse_args(argv)
    if bool(args.counts_dir) == bool(args.report_in):
        parser.error("give exactly one of --counts-dir and --report-in")

    if args.report_in:
        counts_dir, unit_list, skipped, bundled = read_report(Path(args.report_in))
        context = {**bundled, **json.loads(args.context_json)}
    else:
        counts_dir, unit_list = Path(args.counts_dir), _lines(args.units)
        skipped = [line.split("\t") for line in _lines(args.skipped)]
        context = json.loads(args.context_json)
    rows = merge_counts(counts_dir, unit_list) if counts_dir.is_dir() else []
    resources = merge_resources(counts_dir) if counts_dir.is_dir() else {}
    for unit, cost in (merge_costs(counts_dir) if counts_dir.is_dir() else {}).items():
        for row in rows:
            if row["unit"] == unit:
                row["cost"] = cost
    handed_over = False
    # Also with nothing planned: the gate must tell "nothing to apply" from "never planned".
    if args.plans_dir:
        Path(args.plans_dir).mkdir(parents=True, exist_ok=True)
        manifest = join_manifests(Path(args.plans_dir))
        data = metadata(context, rows, manifest, skipped, resources)
        (Path(args.plans_dir) / "metadata.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        handed_over = True
    if args.comment_file:
        Path(args.comment_file).write_text(comment(rows, resources, skipped, args.run_url, context), encoding="utf-8")
    if args.slack_file:
        payload = slack_payload(rows, context, args.run_url, skipped)
        Path(args.slack_file).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.report_out:
        write_report(Path(args.report_out), counts_dir, unit_list, skipped, context)

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"changes={json.dumps(rows, separators=(',', ':'))}\n")
            out.write(f"has_changes={'true' if has_changes(rows) else 'false'}\n")
            out.write(f"handed_over={'true' if handed_over else 'false'}\n")
            out.write(f"quiet={'true' if quiet(rows, skipped) else 'false'}\n")
            out.write(f"working_directory={safe_path(str(context.get('working_directory') or ''))}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
