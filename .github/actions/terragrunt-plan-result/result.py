#!/usr/bin/env python3
"""Merge the plan jobs' counts, hand the plans over to the apply, and render the PR comment.

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


def _count_cells(row: dict) -> str:
    if row.get("kept"):
        return "kept: prevent_destroy | | | | "
    if row.get("error"):
        return f"{row['error']} | | | | "
    return " | ".join(str(row.get(key, 0)) for key in COUNT_KEYS)


def comment(rows: list[dict], resources: dict[str, list[dict[str, str]]], skipped: list[list[str]],
            run_url: str, limit: int = COMMENT_LIMIT) -> str:
    """The sticky PR comment: counts per unit, then each changing unit's addresses and actions."""
    changing = [row for row in rows if any(row.get(key) for key in COUNT_KEYS)]
    head = ["### Terragrunt plan", ""]
    if not rows:
        head.append("No units were planned.")
    elif changing:
        head.append(f"**{len(changing)} of {len(rows)} units would change.** [Run]({run_url})")
    else:
        head.append(f"**No changes** in {len(rows)} units. [Run]({run_url})")
    if rows:
        head += ["", "| Unit | Profile | Add | Change | Replace | Destroy | Outputs |", "|---|---|---|---|---|---|---|"]
        for row in rows:
            unit = f"`{row['unit']}`" + (" (deleted)" if row.get("deleted") else "")
            head.append(f"| {unit} | {row.get('profile', '')} | {_count_cells(row)} |")
    if skipped:
        head += ["", "Not planned: " + ", ".join(f"`{s[0]}` ({s[2]})" for s in skipped if len(s) >= 3)]
    body = "\n".join(head) + "\n"

    sections = []
    for row in changing:
        lines = [f"<details><summary><code>{row['unit']}</code></summary>", "", "```diff"]
        lines += [f"{SIGN[r['action']]} {r['address']}" for r in resources.get(row["unit"], [])]
        lines += ["```", "</details>", ""]
        sections.append("\n".join(lines))
    note = f"\n_Resource lists cut to fit; the full plan is in the [run]({run_url})._\n"
    for index, section in enumerate(sections):
        if len(body) + len(section) + len(note) > limit:
            return body + "\n" + note
        body += ("\n" if index == 0 else "") + section
    return body


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
        Path(args.comment_file).write_text(comment(rows, resources, skipped, args.run_url), encoding="utf-8")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"changes={json.dumps(rows, separators=(',', ':'))}\n")
            out.write(f"has_changes={'true' if has_changes(rows) else 'false'}\n")
            out.write(f"handed_over={'true' if handed_over else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
