#!/usr/bin/env python3
"""What a gcp-nuke plan or apply may touch, and what a scan says it would remove.

gcp-nuke-plan.yaml and gcp-nuke-apply.yaml call this between their gcloud and gcp-nuke
steps; every decision that is not a CLI call lives here so it can be unit tested.

    targets        the projects under `accounts`, refused when one is blocklisted or production
    auto-approve   whether the human gate may be skipped for these targets
    extract        the "would remove" rows of one project's JSON scan log, reconciled with its summary
    errors         the error lines of a scan log, so a partial scan is not silent
    verify         a fresh scan against the approved plan: clean, listing errors, or unapproved rows
    narrow-config  the config with one keep-filter per approved type, so a pass deletes approved names only
    types          the resource types the approved plan lists for one project
    sweep          the leftover projects in a folder, as Project rows
    sweep-approved the approved leftover projects still ACTIVE in the folder, to delete now

A row is `project<TAB>region<TAB>type<TAB>name`. Folder-sweep rows use `folders/<id>` as
their project and `global` as their region. → see docs/gcp-nuke.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SWEEP_TYPE = "Project"
APPROVED_FILTER_VALUE = "gcp-nuke-approved-plan"
SUMMARY_RE = re.compile(r"Scan complete: (\d+) total, (\d+) nukeable, (\d+) filtered")

# verify's exit codes, read by gcp-nuke-apply.yaml.
VERIFY_CLEAN, VERIFY_MALFORMED, VERIFY_LISTING_ERRORS, VERIFY_UNAPPROVED = 0, 1, 2, 3


class ScopeError(Exception):
    """A refusal: the message is printed as a workflow ::error:: and the step fails."""


def split_list(value: str | None) -> list[str]:
    """Comma- or newline-separated values, trimmed, empties dropped."""
    if not value:
        return []
    parts = value.replace("\n", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


def prod_list(value: str | None) -> tuple[bool, list[str]]:
    """(enforced, projects) from RUNESTRA_PROD_PROJECTS; the literal `none` declares none."""
    items = split_list(value)
    if not items:
        return False, []
    if len(items) == 1 and items[0].lower() == "none":
        return True, []
    return True, items


def resolve_targets(config: dict, prod_projects: str | None) -> list[str]:
    accounts = config.get("accounts") or {}
    if not isinstance(accounts, dict) or not accounts:
        raise ScopeError("the config lists no projects under `accounts`")
    blocklist = [str(b) for b in (config.get("blocklist") or [])]
    if not blocklist:
        raise ScopeError("the config's `blocklist` is empty; gcp-nuke requires at least one entry")
    targets = sorted(str(a) for a in accounts)
    # gcp-nuke never reads the blocklist itself, so this check is the only one there is.
    blocked = [t for t in targets if t in blocklist]
    if blocked:
        raise ScopeError(f"blocklisted project(s) also under `accounts`: {', '.join(blocked)}")
    _, prod = prod_list(prod_projects)
    production = [t for t in targets if t in prod]
    if production:
        raise ScopeError(f"production project(s) under `accounts` (RUNESTRA_PROD_PROJECTS): {', '.join(production)}")
    return targets


def auto_approve(requested: bool, targets: list[str], sweep_folder: str, allowlist: str | None,
                 prod_projects: str | None, app_configured: bool) -> tuple[bool, str]:
    """(allowed, why). Every condition is required; a missing App is a hard error, not a fallback."""
    if not requested:
        return False, "auto-approve not requested"
    enforced, prod = prod_list(prod_projects)
    if not enforced:
        return False, "RUNESTRA_PROD_PROJECTS is unset, so the production guard cannot enforce"
    production = [t for t in targets if t in prod]
    if production:
        raise ScopeError(f"production project(s) requested for auto-approve: {', '.join(production)}")
    allow = split_list(allowlist)
    if not allow:
        return False, "NUKE_AUTO_APPROVE_PROJECTS is unset"
    needed = list(targets) + ([f"folders/{sweep_folder}"] if sweep_folder else [])
    missing = [n for n in needed if n not in allow]
    if missing:
        return False, f"not on NUKE_AUTO_APPROVE_PROJECTS: {', '.join(missing)}"
    if not app_configured:
        # Without an App token the approve comment fires nothing, so auto-approve would be a silent no-op.
        raise ScopeError("auto_approve requires github_client_id (or github_app_id) and github_app_key")
    return True, "every target is on NUKE_AUTO_APPROVE_PROJECTS"


def log_entries(path: Path):
    """The JSON objects of a gcp-nuke log; its prompt and banner lines are plain text."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                yield entry


def scan_summary(path: Path) -> int | None:
    """The nukeable count of the scan's own `Scan complete` line, JSON or plain text."""
    nukeable = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = SUMMARY_RE.search(line)
            if match:
                nukeable = int(match.group(2))
    return nukeable


def extract_rows(project: str, path: Path) -> list[str]:
    """Every would-remove event as a row; a malformed event or a count off the summary fails."""
    rows = set()
    events = 0
    for entry in log_entries(path):
        if not str(entry.get("msg", "")).lower().startswith("would remove"):
            continue
        events += 1
        region = str(entry.get("owner") or "")
        rtype = str(entry.get("type") or "")
        name = str(entry.get("name") or "")
        if not rtype or not name or "\t" in name or "\n" in name or "\t" in region:
            raise ScopeError(f"{project}: a would-remove event without a usable type or name: {json.dumps(entry)[:300]}")
        rows.add("\t".join((project, region, rtype, name)))
    nukeable = scan_summary(path)
    if nukeable is None:
        raise ScopeError(f"{project}: the scan log has no `Scan complete` summary; the log format may have changed")
    if nukeable != events:
        raise ScopeError(f"{project}: gcp-nuke counted {nukeable} nukeable, the log holds {events} would-remove events")
    return sorted(rows)


def error_lines(path: Path) -> list[str]:
    errors = []
    for entry in log_entries(path):
        if str(entry.get("level", "")).lower() in ("error", "fatal", "panic"):
            text = " ".join(str(entry.get("msg", "")).split())
            rtype = entry.get("resource_type") or entry.get("resource") or ""
            errors.append(f"{rtype}: {text}"[:300] if rtype else text[:300])
    return errors


def read_rows(path: Path) -> list[str]:
    return [line.rstrip("\n") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def unapproved_rows(approved: list[str], project: str, path: Path) -> list[str]:
    ok = set(approved)
    return [row for row in extract_rows(project, path) if row not in ok]


def verify(approved: list[str], project: str, path: Path) -> tuple[int, list[str]]:
    """(exit code, lines): listing errors are retryable, unapproved rows are not."""
    errors = error_lines(path)
    if errors:
        return VERIFY_LISTING_ERRORS, errors
    try:
        extra = unapproved_rows(approved, project, path)
    except ScopeError as err:
        return VERIFY_MALFORMED, [str(err)]
    if extra:
        return VERIFY_UNAPPROVED, extra
    return VERIFY_CLEAN, []


def approved_types(approved: list[str], project: str) -> list[str]:
    types = set()
    for row in approved:
        parts = row.split("\t")
        if len(parts) == 4 and parts[0] == project and parts[2]:
            types.add(parts[2])
    return sorted(types)


def narrow_config(config: dict, approved: list[str], project: str) -> dict:
    """The config plus, per approved type, a keep-filter on every name the plan did not approve.

    libnuke ORs filters of one group, so the config's own keep-filters still keep. Its filter
    validation rejects an empty value, hence the inert value on a NotIn filter.
    """
    names: dict[str, set[str]] = {}
    for row in approved:
        parts = row.split("\t")
        if len(parts) == 4 and parts[0] == project and parts[2]:
            names.setdefault(parts[2], set()).add(parts[3])
    accounts = config.setdefault("accounts", {}) or {}
    config["accounts"] = accounts
    if project not in accounts:
        raise ScopeError(f"{project} is not under `accounts`")
    account = accounts[project] or {}
    accounts[project] = account
    filters = account.get("filters") or {}
    account["filters"] = filters
    for rtype, approved_names in sorted(names.items()):
        keep = filters.get(rtype) or []
        keep.append({"type": "NotIn", "values": sorted(approved_names), "value": APPROVED_FILTER_VALUE})
        filters[rtype] = keep
    return config


def parse_time(value: str) -> datetime:
    # gcloud prints RFC 3339 with a Z and up to nanoseconds; fromisoformat takes microseconds.
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, rest = text.split(".", 1)
        digits = "".join(ch for ch in rest if ch.isdigit())
        tail = rest[len(digits):]
        text = f"{head}.{digits[:6].ljust(6, '0')}{tail}"
    stamp = datetime.fromisoformat(text)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def in_folder(project: dict, folder: str) -> bool:
    parent = project.get("parent") or {}
    return not parent or (parent.get("type") == "folder" and str(parent.get("id")) == folder)


def refuse_production_in_folder(projects: list[dict], folder: str, prod_projects: str | None) -> list[str]:
    """The production list; any production project inside the sweep folder fails the run."""
    _, prod = prod_list(prod_projects)
    found = sorted(str(p.get("projectId")) for p in projects if in_folder(p, folder) and p.get("projectId") in prod)
    if found:
        raise ScopeError(f"production project(s) inside sweep folder {folder}: {', '.join(found)}")
    return prod


def sweep_rows(projects: list[dict], folder: str, min_age_hours: float, protect: list[str], now: datetime,
               prod_projects: str | None = None) -> list[str]:
    protected = set(protect) | set(refuse_production_in_folder(projects, folder, prod_projects))
    rows = set()
    for project in projects:
        pid = str(project.get("projectId") or "")
        # DELETE_REQUESTED is already gone: GCP purges it after 30 days.
        if not pid or project.get("lifecycleState") != "ACTIVE" or pid in protected or not in_folder(project, folder):
            continue
        created = project.get("createTime")
        if not created:
            continue
        age_hours = (now - parse_time(str(created))).total_seconds() / 3600
        if age_hours >= min_age_hours:
            rows.add("\t".join((f"folders/{folder}", "global", SWEEP_TYPE, pid)))
    return sorted(rows)


def sweep_approved(approved: list[str], projects: list[dict], folder: str, protect: list[str],
                   prod_projects: str | None) -> list[str]:
    """Approved Project rows of this folder still ACTIVE there; a protected one fails the run."""
    prod = refuse_production_in_folder(projects, folder, prod_projects)
    protected = set(protect) | set(prod)
    active = {str(p.get("projectId")) for p in projects if p.get("lifecycleState") == "ACTIVE" and in_folder(p, folder)}
    planned = []
    for row in approved:
        parts = row.split("\t")
        if len(parts) == 4 and parts[0] == f"folders/{folder}" and parts[2] == SWEEP_TYPE:
            planned.append(parts[3])
    bad = sorted(p for p in planned if p in protected)
    if bad:
        raise ScopeError(f"the plan would delete protected project(s): {', '.join(bad)}")
    return sorted(p for p in set(planned) if p in active)


def flag(value: str) -> bool:
    return value.strip().lower() == "true"


def main(argv: list[str] | None = None) -> int:  # noqa: C901
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("targets")
    p.add_argument("--config-json", type=Path, required=True)
    p.add_argument("--prod-projects", default="")

    p = sub.add_parser("auto-approve")
    p.add_argument("--requested", required=True)
    p.add_argument("--targets-file", type=Path, required=True)
    p.add_argument("--sweep-folder", default="")
    p.add_argument("--allowlist", default="")
    p.add_argument("--prod-projects", default="")
    p.add_argument("--app-configured", default="false")

    for name in ("extract", "unapproved", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        p.add_argument("--log", type=Path, required=True)
        if name != "extract":
            p.add_argument("--approved", type=Path, required=True)

    p = sub.add_parser("errors")
    p.add_argument("--log", type=Path, required=True)

    p = sub.add_parser("types")
    p.add_argument("--approved", type=Path, required=True)
    p.add_argument("--project", required=True)

    p = sub.add_parser("narrow-config")
    p.add_argument("--config-json", type=Path, required=True)
    p.add_argument("--approved", type=Path, required=True)
    p.add_argument("--project", required=True)

    for name in ("sweep", "sweep-approved"):
        p = sub.add_parser(name)
        p.add_argument("--projects-json", type=Path, required=True)
        p.add_argument("--folder", required=True)
        p.add_argument("--protect", action="append", default=[])
        p.add_argument("--prod-projects", default="")
        if name == "sweep":
            p.add_argument("--min-age-hours", type=float, required=True)
            p.add_argument("--now", default="", help="RFC 3339 timestamp, for tests")
        else:
            p.add_argument("--approved", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "targets":
            out = resolve_targets(json.loads(args.config_json.read_text(encoding="utf-8")) or {}, args.prod_projects)
        elif args.command == "auto-approve":
            allowed, why = auto_approve(flag(args.requested), read_rows(args.targets_file), args.sweep_folder,
                                        args.allowlist, args.prod_projects, flag(args.app_configured))
            print(why, file=sys.stderr)
            out = ["true" if allowed else "false"]
        elif args.command == "extract":
            out = extract_rows(args.project, args.log)
        elif args.command == "errors":
            out = error_lines(args.log)
        elif args.command == "unapproved":
            out = unapproved_rows(read_rows(args.approved), args.project, args.log)
        elif args.command == "verify":
            code, lines = verify(read_rows(args.approved), args.project, args.log)
            for line in lines:
                print(line)
            return code
        elif args.command == "types":
            out = approved_types(read_rows(args.approved), args.project)
        elif args.command == "narrow-config":
            config = json.loads(args.config_json.read_text(encoding="utf-8")) or {}
            out = [json.dumps(narrow_config(config, read_rows(args.approved), args.project), indent=2)]
        else:
            if not args.folder.isdigit():
                raise ScopeError(f"folder ID must be numeric, got {args.folder!r}")
            projects = json.loads(args.projects_json.read_text(encoding="utf-8")) or []
            protect = [p for item in args.protect for p in split_list(item)]
            if args.command == "sweep":
                now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
                out = sweep_rows(projects, args.folder, args.min_age_hours, protect, now, args.prod_projects)
            else:
                out = sweep_approved(read_rows(args.approved), projects, args.folder, protect, args.prod_projects)
    except ScopeError as err:
        print(f"::error::{err}", file=sys.stderr)
        return 1
    for line in out:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
