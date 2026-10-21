#!/usr/bin/env python3
"""Check an approval against the saved plans, then apply exactly those plans.

`approval` validates an `/approve apply-<id>` (or `destroy-<id>`) comment on a gate issue; `plans` checks the
downloaded metadata.json against that approval, or against an expectations policy; `run`
re-plans each unit and compares changesets (`check`), applies the saved plan files
(`apply`), proves a second plan is empty (`clean`), plans, checks and applies each
unit in dependency order with no saved plan (`fresh`), or plans, checks and destroys each
unit in reverse dependency order (`destroy`). It lives beside its action, not in
scripts/, because the public mirror copies actions only. → docs/terragrunt-apply.md

    python3 .github/actions/terragrunt-apply-units/apply_units.py approval --approvers "$APPROVERS"
    python3 .github/actions/terragrunt-apply-units/apply_units.py run --phase check --units "$UNITS" ...
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MARKER = re.compile(r"<!-- terragrunt-apply: (\{.*?\}) -->")
APPROVE = re.compile(r"^\s*/approve\s+(apply|destroy)-(\d+)\s*$")
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


def state_changes(plan: dict) -> int:
    """Resources the plan imports, moves or forgets: no count shows them, but the apply changes state."""
    return sum(1 for c in plan.get("resource_changes") or [] if c.get("previous_address")
               or (c.get("change") or {}).get("importing") or "forget" in (c.get("change") or {}).get("actions", []))


def changing(unit: dict) -> bool:
    """A saved plan's unit to apply: a count, or state_changes, absent from older metadata. → docs/terragrunt-plan.md"""
    return any(unit.get(k) for k in COUNT_KEYS) or bool(unit.get("state_changes"))


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
        raise Refused("the comment is not exactly `/approve apply-<artifact id>` or `/approve destroy-<artifact id>`")
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
    verb, artifact_id = match.group(1), match.group(2)
    if verb != marker.get("action", "apply"):
        raise Refused(f"the comment approves an {verb}, the issue gates an {marker.get('action', 'apply')}")
    if str(marker.get("artifact_id")) != artifact_id:
        raise Refused(f"the comment approves artifact {artifact_id}, the issue gates {marker.get('artifact_id')}")
    return {**marker, "actor": actor, "issue": issue.get("number")}


def check_plans(metadata: dict, marker: dict | None, artifact: dict | None, commit_tree: str) -> None:
    """The downloaded plans are the ones approved, for the commit about to be checked out."""
    if metadata.get("schema") != 1:
        raise Refused(f"metadata.json schema {metadata.get('schema')!r} is not one this apply reads")
    if metadata.get("tree") != commit_tree:
        raise Refused("the checked-out code differs from the code that was planned")
    if marker is None:
        return
    if bool(metadata.get("destroy")) != (marker.get("action") == "destroy"):
        raise Refused("the plans and the approved issue disagree on whether this is a destroy")
    if artifact is None or artifact.get("expired"):
        raise Refused("the plan artifact expired; re-plan the change")
    if (artifact.get("workflow_run") or {}).get("id") != marker["plan_run_id"]:
        raise Refused("the artifact does not come from the plan run the gate recorded")
    for key in ("plan_run_id", "head_sha", "tree", "pr"):
        if metadata.get(key) != marker.get(key):
            raise Refused(f"metadata.json {key} {metadata.get(key)!r} differs from the gate's {marker.get(key)!r}")


def check_policy(expect: dict) -> None:
    """Typos in a policy fail before anything is planned."""
    known = {"allow_replace", "allow_destroy", "units", "require_changes", "max_monthly_cost_delta"}
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


def destroy_violation(unit: dict, expect: dict) -> str:
    """Why one unit's destroy plan breaks the policy; a destroy plan only ever deletes."""
    for key in ("add", "change", "replace"):
        if unit.get(key):
            return f"{unit['unit']} would {key} {unit[key]} resources while being destroyed"
    return unit_violation(unit, {**expect, "allow_destroy": True})


def check_expectations(metadata: dict, expect: dict) -> None:
    """An automatic apply's policy: which actions it may take, and optionally exact counts."""
    check_policy(expect)
    if metadata.get("destroy"):
        raise Refused("these are destroy plans, which an automatic apply never runs")
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
    if "max_monthly_cost_delta" in expect:
        check_cost(metadata, float(expect["max_monthly_cost_delta"]))


def check_cost(metadata: dict, limit: float) -> None:
    """The estimated monthly cost may grow by at most limit; an unknown cost is not a small one."""
    changing = [u for u in metadata.get("units", []) if any(u.get(k) for k in COUNT_KEYS)]
    priced = [u for u in changing if (u.get("cost") or {}).get("estimate")]
    unknown = [u["unit"] for u in changing if not (u.get("cost") or {}).get("estimate")]
    if not any(isinstance(u.get("cost"), dict) for u in changing) and changing:
        raise Refused("max_monthly_cost_delta is set but the plan has no cost estimate; give the plan an Infracost key")
    if unknown:
        raise Refused("max_monthly_cost_delta is set but these units could not be estimated: " + ", ".join(unknown))
    total = round(sum(float(u["cost"]["delta"]) for u in priced), 2)
    if total > limit:
        raise Refused(f"the plan adds {total:,.2f} per month, more than max_monthly_cost_delta {limit:,.2f}")


def second_approver_reason(metadata: dict, threshold: float) -> str:
    """Why this apply needs a second approver; empty when one is enough. Unknown cost counts as over."""
    changing = [u for u in metadata.get("units", []) if any(u.get(k) for k in COUNT_KEYS)]
    if metadata.get("destroy") or not changing:
        return ""
    unpriced = [u["unit"] for u in changing if not (u.get("cost") or {}).get("estimate")]
    if unpriced:
        return "these units have no cost estimate: " + ", ".join(unpriced)
    total = round(sum(float(u["cost"]["delta"]) for u in changing), 2)
    if total > threshold:
        return f"it adds {total:,.2f} per month, more than the two-approver threshold of {threshold:,.2f}"
    return ""


def approvals(comments: list[dict], marker: dict, approvers: list[str]) -> list[str]:
    """Distinct allowed approvers whose unedited comment is exactly this issue's approve command."""
    wanted = f"{marker.get('action', 'apply')}-{marker.get('artifact_id')}"
    seen: list[str] = []
    for comment in comments:
        match = APPROVE.match(comment.get("body") or "")
        login = ((comment.get("user") or {}).get("login") or "").lower()
        edited = comment.get("updated_at") not in (None, comment.get("created_at"))
        if match and f"{match.group(1)}-{match.group(2)}" == wanted and login in approvers and not edited \
                and login not in seen:
            seen.append(login)
    return seen


def find_newest(unit_dir: Path, name: str) -> Path | None:
    found = sorted(unit_dir.glob(f".terragrunt-cache/**/{name}"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def terragrunt(unit_dir: Path, *args: str) -> int:
    print(f"::group::{unit_dir}: {' '.join(args)}", flush=True)
    code = subprocess.run(["terragrunt", "run", "--non-interactive", "--", *args], cwd=unit_dir, check=False).returncode
    print("::endgroup::", flush=True)
    return code


def run_check(root: Path, units: list[dict], engine: str, destroy: bool = False) -> None:
    """Re-plan every unit before anything is applied or destroyed; any difference is drift."""
    drifted = []
    for unit in units:
        unit_dir = root / unit["unit"]
        mode = ["-destroy"] if destroy else []
        if terragrunt(unit_dir, "plan", *mode, "-lock=false", "-input=false", "-out=tfplan-check") != 0:
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
        # A saved plan never refreshes, so files written at plan time (an archive_file zip) are missing on this runner.
        # A full plan, not -refresh-only: that one plans no creates, so a not-yet-created resource reads as null.
        if terragrunt(root / unit["unit"], "plan", "-lock=false", "-input=false") != 0:
            raise Refused(f"{unit['unit']} could not be planned before its apply; the units after it were not touched")
        if terragrunt(root / unit["unit"], "apply", "-input=false", str(plan_file.resolve())) != 0:
            raise Refused(f"{unit['unit']} failed to apply; the units after it were not touched")
        print(f"{unit['unit']}: applied")


def run_destroy(root: Path, units: list[dict]) -> None:
    """In the order planned, dependents first; `destroy`, not the plan file, so destroy hooks run."""
    for unit in units:
        if terragrunt(root / unit["unit"], "destroy", "-auto-approve", "-input=false") != 0:
            raise Refused(f"{unit['unit']} failed to destroy; the units after it were not touched")
        print(f"{unit['unit']}: destroyed")


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


_EMIT = threading.Lock()


class NullSink:
    """What a run without console_url uses: nothing is sent and nothing is touched."""

    enabled = False

    def emit(self, event_type: str, payload: dict | None = None) -> bool:
        return False

    def close(self) -> None:
        return None


def build_sink() -> object:
    """An EventSink from the CONSOLE_* environment, or a NullSink when console_url is empty."""
    if not os.environ.get("CONSOLE_URL", ""):
        return NullSink()
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console-events"))
        from console_events import EventSink
    except ImportError:
        return NullSink()
    return EventSink(os.environ["CONSOLE_URL"], os.environ.get("CONSOLE_AUDIENCE", ""),
                     os.environ.get("CONSOLE_RUN_ID", ""))


def emit(sink: object, event_type: str, payload: dict) -> None:
    """Parallel units share one sink; telemetry never fails an apply."""
    if sink is None:
        return
    try:
        with _EMIT:
            sink.emit(event_type, payload)
    except Exception:
        pass


def log_lines_wanted() -> bool:
    """console_log_lines: on by default once console_url is set."""
    return os.environ.get("CONSOLE_LOG_LINES", "").strip().lower() not in ("false", "0", "no", "off")


def reporting(sink: object) -> bool:
    return bool(getattr(sink, "enabled", False))


def dag_units(root: Path, order: list[str], queue_as: str = "") -> list[str]:
    """The selected units in `terragrunt find --dag --json` order (or `--queue-construct-as`), relative to root; any it
    did not list follow."""
    sort = ["--queue-construct-as", queue_as] if queue_as else ["--dag"]
    try:
        found = subprocess.run(["terragrunt", "find", *sort, "--json"], cwd=root.resolve(),
                               capture_output=True, text=True, check=True).stdout
        paths = [os.path.normpath(r["path"]) for r in json.loads(found) if r.get("type") == "unit"]
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
        paths = []
    chosen = set(order)
    ordered = [p for p in dict.fromkeys(paths) if p in chosen]
    return ordered + [u for u in order if u not in set(ordered)]


_PRINT = threading.Lock()


LOG_LINE_CAP = 4096
ERROR_EXCERPT_CAP = 4096
PROGRESS_STATUS = {"apply_start": "started", "apply_progress": "progressing", "apply_complete": "completed",
                   "apply_errored": "errored"}
PROGRESS_POLL_SECONDS = 0.5


ANSI_SEQUENCE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\^_])")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
TERRAGRUNT_PREFIX = re.compile(r"^\s*(?:(?:\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+)?(?P<stream>(?:STDOUT|STDERR)\s+\S+:)\s?|"
                               r"\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+(?P<level>TRACE|DEBUG|INFO|WARN|ERROR)\b)")
BOX_GUTTER = re.compile(r"^\s*([\u2577\u2502\u2575])\s?")
ERROR_LINE = re.compile(r"\s*Error:")
DIAGNOSTIC = re.compile(r"\s*(?:Error|Warning):")
INIT_TAG = re.compile(r"^\s*(?:\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+)?INFO\s+(?:tofu|terraform):")
INIT_SOURCE = re.compile(r"^\s*(?:\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+)?INFO\s+Downloading Terraform configurations from ")
COMMAND_WORD = re.compile(r"[a-z][a-z0-9-]{0,31}")
ERROR_EXCERPT_MAX_LINES = 100


def split_prefix(text: str) -> tuple[str, str]:
    """Terragrunt's log prefix (`STDERR tofu:` or a level such as `ERROR`) and the text after it."""
    prefix = TERRAGRUNT_PREFIX.match(text)
    if prefix is None:
        return "", text
    if prefix.group("stream"):
        return " ".join(prefix.group("stream").split()), text[prefix.end():]
    level, rest = prefix.group("level"), text[prefix.end():]
    pad = len(rest) - len(rest.lstrip(" "))
    return level, rest[min(pad, 7 - len(level)):]  # the level is padded to six columns, then one space


class ErrorExcerptCollector:
    """Every error block of a command's output, for the failed-unit event. → docs/console-integration.md"""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._blocks: list[list[str]] = []
        self._banked = ""
        self._size = 0
        self._count = 0
        self._overflow = False
        self._state = "waiting"
        self._source = ""
        self._previous = ""
        self._blank = False
        self._mode = ""
        self._pending = 0
        self._fallback: tuple[list[str], bool] | None = None
        self._broken = False

    def feed(self, line: str) -> None:
        """Best effort: a bug here ends the capture, never the apply."""
        if self._broken:
            return
        try:
            self._consume(line)
        except Exception:
            self._broken = True

    def _consume(self, line: str) -> None:
        source, text = split_prefix(CONTROL_CHARACTERS.sub("", ANSI_SEQUENCE.sub("", line)).rstrip("\n"))
        previous, self._previous = self._previous, source
        if self._state == "done":
            return
        gutter = BOX_GUTTER.match(text)
        boxed = gutter is not None and gutter.group(1) == "\u2502"
        content = text[gutter.end():] if boxed else text
        if self._state == "waiting":
            self._start(source, boxed, text, content)
        elif self._state == "seeking":
            if source == "ERROR" and previous != "ERROR" and text.strip() and not ERROR_LINE.match(text):
                self._state = "native"
                self._add(text.strip())
        elif self._state == "boxed":
            if self._boxed(boxed, content):
                self._resume(source, boxed, text, content)
        elif self._state == "plain":
            if self._plain(source, text):
                self._resume(source, boxed, text, content)
        else:
            self._native(source, text)

    def _resume(self, source: str, boxed: bool, text: str, content: str) -> None:
        """The line that ended a block may open the next one."""
        if self._state == "waiting":
            self._start(source, boxed, text, content)

    def _start(self, source: str, boxed: bool, text: str, content: str) -> None:
        if self._banked and source == "ERROR" and not boxed and text.strip():
            self._state = "done"  # Terragrunt's own message restates the blocks already banked
            return
        if ERROR_LINE.match(content):
            self._state, self._source = ("boxed" if boxed else "plain"), source
            self._add(content if boxed else text)
        elif source == "ERROR" and text.strip():
            self._state = "native"
            self._add(text.strip())

    def _boxed(self, boxed: bool, content: str) -> bool:
        if not boxed:
            self._end()
            return True
        self._add(content)
        return False

    def _plain(self, source: str, content: str) -> bool:
        """Ends at the next diagnostic, another log source or two blank lines: no-color output has single ones."""
        blank = not content.strip()
        if source != self._source or DIAGNOSTIC.match(content) or (blank and self._blank):
            self._end()
            return True
        self._blank = blank
        self._add(content)
        return False

    def _native(self, source: str, content: str) -> None:
        """Terragrunt's own message: `error occurred:` up to its last `* ` item, any other one up to the next log line."""
        blank = not content.strip()
        if source:
            self._end()
        elif not self._mode and blank:
            self._pending += 1
            if self._pending > ERROR_EXCERPT_MAX_LINES:
                self._end()
        elif not self._mode:
            self._mode = "items" if content.startswith("* ") else "message"
            for _ in range(self._pending if self._mode == "message" else 0):
                self._add("")
            self._add(content)
        elif self._mode == "items" and (blank or not (content.startswith("* ") or content[:1].isspace())):
            self._end()
        else:
            self._add(content)

    def _end(self) -> None:
        """A Terragrunt-level HCL diagnostic is kept only until Terragrunt's own error, which names the cause, follows."""
        if self._state == "plain" and self._source == "ERROR" and self._fallback is None:
            self._fallback = (self._lines, self._overflow)
            self._lines, self._size, self._count, self._overflow = [], 0, 0, False
            self._state = "seeking"
            return
        if self._lines:
            self._blocks.append(self._lines)
            self._count += 1  # the blank line that joins it to the next block
            self._size += 1
        self._banked = self._banked or ("native" if self._state == "native" else "diagnostic")
        self._lines, self._mode, self._pending, self._blank = [], "", 0, False
        self._state = "done" if self._banked == "native" else "waiting"

    def _add(self, content: str) -> None:
        if self._state == "done":
            return
        self._count += 1
        if self._size >= 2 * ERROR_EXCERPT_CAP or self._count > ERROR_EXCERPT_MAX_LINES:
            self._overflow = True
            self._end()
            if self._state == "waiting":
                self._state = "done"
            return
        self._lines.append(content.rstrip())
        self._size += len(content.encode("utf-8", "replace")) + 1

    def result(self, mask: object = None) -> tuple[str, bool] | None:
        """The masked, capped text and whether it was cut; None when there was no error block."""
        try:
            blocks = self._blocks + ([self._lines] if self._lines and not self._broken else [])
            overflow = self._overflow or self._broken
            if not blocks and not self._broken:
                lines, overflow = self._fallback or ([], False)
                blocks = [lines] if lines else []
            if not blocks:
                return None
            text = "\n\n".join("\n".join(block).rstrip() for block in blocks)
            if callable(mask):
                text = mask(text)
            raw = text.encode("utf-8", "replace")
            return raw[:ERROR_EXCERPT_CAP].decode("utf-8", "ignore"), overflow or len(raw) > ERROR_EXCERPT_CAP
        except Exception:
            return None


def command_of(args: tuple[str, ...]) -> str | None:
    """The engine command a streamed() call invokes (its first non-flag word), or None outside COMMAND_WORD."""
    word = next((arg for arg in args if not arg.startswith("-")), "")
    return word if COMMAND_WORD.fullmatch(word) else None


def line_command(line: str, command: str | None) -> str | None:
    """`init` for terragrunt's auto-init output, else the invoked command. → docs/console-integration.md"""
    if command is None:
        return None
    plain = ANSI_SEQUENCE.sub("", line)
    return "init" if INIT_TAG.match(plain) or INIT_SOURCE.match(plain) else command


def streamed(unit: str, cwd: Path, *args: str, sink: object = None, log_lines: bool = False,
             collector: ErrorExcerptCollector | None = None, env: dict[str, str] | None = None) -> int:
    """terragrunt run in one unit, its output prefixed so parallel units stay readable; env adds to this call only."""
    process = subprocess.Popen(["terragrunt", "run", "--non-interactive", "--", *args], cwd=cwd,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               env={**os.environ, **env} if env else None)
    assert process.stdout is not None
    command = command_of(args)
    for line in process.stdout:
        with _PRINT:
            print(f"[{unit}] {line}", end="", flush=True)
        if collector is not None:
            collector.feed(line)
        if log_lines and reporting(sink) and line.strip():
            masked = sink.mask(line.rstrip("\r\n")) if hasattr(sink, "mask") else line.rstrip("\r\n")
            text = masked.encode("utf-8", "replace")[:LOG_LINE_CAP].decode("utf-8", "ignore")
            payload = {"stream": "stdout", "level": "info", "text": text, "unit": unit}
            tag = line_command(line, command)
            emit(sink, "log.line", {**payload, "command": tag} if tag else payload)
    return process.wait()


def progress_event(unit: str, raw: str) -> dict | None:
    """One `-json-into` line as an opentofu.resource.progress payload; None for anything else."""
    try:
        message = json.loads(raw)
        status = PROGRESS_STATUS.get(message["type"])
        hook = message["hook"]
        address, action = hook["resource"]["addr"], hook.get("action", "")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if status is None or not isinstance(address, str) or not isinstance(action, str):
        return None
    return {"unit": unit, "address": address, "action": action, "status": status}


class ResourceProgress:
    """Tails OpenTofu's `-json-into` file while an apply runs and forwards resource progress to the sink."""

    def __init__(self, unit: str, sink: object) -> None:
        self.unit, self.sink = unit, sink
        self.path = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ResourceProgress":
        handle, self.path = tempfile.mkstemp(prefix="tofu-json-", suffix=".json")
        os.close(handle)
        self._thread = threading.Thread(target=self._tail, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _forward(self, raw: str) -> None:
        payload = progress_event(self.unit, raw)
        if payload is not None:
            emit(self.sink, "opentofu.resource.progress", payload)

    def _tail(self) -> None:
        pending = ""
        try:
            with open(self.path, encoding="utf-8", errors="replace") as handle:
                while True:
                    finished = self._stop.is_set()
                    data = handle.read()
                    pending += data
                    *lines, pending = pending.split("\n")
                    for raw in lines:
                        self._forward(raw)
                    if finished and not data:
                        break
                    if not data:
                        self._stop.wait(PROGRESS_POLL_SECONDS)
            if pending.strip():
                self._forward(pending)
        except Exception:  # a broken tail must never reach the apply
            pass


@functools.lru_cache(maxsize=None)
def supports_json_into(engine: str) -> bool:
    """Whether `<engine> apply` takes `-json-into`: OpenTofu 1.13+ only, never terraform. → docs/console-integration.md"""
    try:
        shown = subprocess.run([engine, "apply", "-help"], capture_output=True, text=True, check=False, timeout=30)
    except (subprocess.SubprocessError, OSError, ValueError):
        return False
    return "-json-into" in f"{shown.stdout or ''}{shown.stderr or ''}"


def excerpt_row(collector: ErrorExcerptCollector | None, sink: object) -> dict:
    """The collector's result as a transient row key that fresh_unit moves onto the failed event."""
    found = collector.result(getattr(sink, "mask", None)) if collector is not None else None
    return {"excerpt": {"text": found[0], "shortened": found[1]}} if found else {}


def fresh_steps(root: Path, unit: str, expect: dict, engine: str, sink: object = None,
                log_lines: bool = False) -> dict:
    """Plan one unit now, check the plan against the policy, then apply exactly that plan."""
    unit_dir = root / unit
    collector = ErrorExcerptCollector() if reporting(sink) and log_lines else None
    if streamed(unit, unit_dir, "plan", "-input=false", "-out=tfplan-fresh", sink=sink, log_lines=log_lines,
                collector=collector) != 0:
        return {"unit": unit, "outcome": "plan failed", **excerpt_row(collector, sink)}
    plan_file = find_newest(unit_dir, "tfplan-fresh")
    if plan_file is None:
        return {"unit": unit, "outcome": "left no plan"}
    shown = subprocess.run([engine, "show", "-json", plan_file.name], cwd=plan_file.parent,
                           capture_output=True, text=True, check=True)
    plan = json.loads(shown.stdout)
    row = {"unit": unit, **counts_of(resources(plan))}
    violation = unit_violation(row, expect)
    if violation:
        return {**row, "outcome": "refused", "reason": violation}
    if not any(row[key] for key in COUNT_KEYS) and not state_changes(plan):
        return {**row, "outcome": "unchanged"}
    collector = ErrorExcerptCollector() if reporting(sink) and log_lines else None
    if reporting(sink) and supports_json_into(engine):
        with ResourceProgress(unit, sink) as progress:
            code = streamed(unit, unit_dir, "apply", "-input=false", f"-json-into={progress.path}",
                            str(plan_file.resolve()), sink=sink, log_lines=log_lines, collector=collector)
    elif reporting(sink):
        code = streamed(unit, unit_dir, "apply", "-input=false", str(plan_file.resolve()), sink=sink,
                        log_lines=log_lines, collector=collector)
    else:
        code = streamed(unit, unit_dir, "apply", "-input=false", str(plan_file.resolve()))
    if code != 0:
        return {**row, "outcome": "apply failed", **excerpt_row(collector, sink)}
    return {**row, "outcome": "applied"}


FAILURE_REASONS = {"plan failed": "plan_failed", "left no plan": "plan_failed", "refused": "expectation_violated",
                   "apply failed": "apply_failed"}


def fresh_unit(root: Path, unit: str, expect: dict, engine: str, sink: object = None,
               log_lines: bool = False) -> dict:
    """fresh_steps, reporting the unit's start and end from its own result row."""
    emit(sink, "terragrunt.unit.started", {"unit": unit})
    try:
        row = fresh_steps(root, unit, expect, engine, sink, log_lines)
    except Exception:
        emit(sink, "terragrunt.unit.failed", {"unit": unit, "reason": "plan_failed"})
        raise
    excerpt = row.pop("excerpt", None)
    if row["outcome"] in ("applied", "unchanged"):
        emit(sink, "terragrunt.unit.completed", {"unit": unit, "resources_added": row.get("add", 0),
                                                 "change": row.get("change", 0), "replace": row.get("replace", 0),
                                                 "destroy": row.get("destroy", 0), "outcome": row["outcome"]})
    else:
        failed = {"unit": unit, "reason": FAILURE_REASONS.get(row["outcome"], "plan_failed")}
        if isinstance(excerpt, dict):
            failed |= {"error_excerpt": excerpt["text"], "error_excerpt_shortened": excerpt["shortened"]}
        emit(sink, "terragrunt.unit.failed", failed)
    return row


HOOK_REPORT_VARIABLE = "RUNESTRA_HOOK_REPORT_FILE"
HOOK_REPORT_MAX_LINES = 32
HOOK_REPORT_MAX_LINE_BYTES = 8192
HOOK_REPORT_MAX_ITEMS = 200
_HOOK_REPORTS = itertools.count(1)
_HOOK_REPORTS_LOCK = threading.Lock()


def hook_report_path() -> Path | None:
    """A report file of its own for one destroy invocation (parallel units share the process environment); None when
    the directory cannot be made, so a report problem never stops the destroy."""
    with _HOOK_REPORTS_LOCK:
        number = next(_HOOK_REPORTS)
    directory = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "runestra-hook-reports"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{number}.ndjson").unlink(missing_ok=True)
    except OSError:
        return None
    return directory / f"{number}.ndjson"


def hook_items(value: object) -> list[dict[str, str]] | None:
    if not isinstance(value, list) or len(value) > HOOK_REPORT_MAX_ITEMS:
        return None
    if not all(isinstance(i, dict) and isinstance(i.get("kind"), str) and isinstance(i.get("id"), str) for i in value):
        return None
    return [{"kind": item["kind"], "id": item["id"]} for item in value]


def hook_report(raw: bytes) -> dict | None:
    """One report line as the event payload without `unit`; None unless it is exactly the documented shape."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict) or type(body.get("version")) is not int or body["version"] != 1:
        return None
    removed, remaining = hook_items(body.get("removed")), hook_items(body.get("remaining"))
    reason, truncated = body.get("skipped_reason", 0), body.get("truncated", False)
    if not isinstance(body.get("hook"), str) or removed is None or remaining is None \
            or not (reason is None or isinstance(reason, str)) or not isinstance(truncated, bool):
        return None
    report = {"hook": body["hook"], "removed": removed, "remaining": remaining, "skipped_reason": reason}
    return {**report, "truncated": truncated} if "truncated" in body else report


def read_hook_reports(path: Path) -> tuple[list[dict], int]:
    """The valid reports in the file and how many lines were dropped; reads at most the capped lines and bytes."""
    reports: list[dict] = []
    dropped = seen = 0
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return [], 0
    with handle:
        while raw := handle.readline(HOOK_REPORT_MAX_LINE_BYTES + 1):
            oversized = len(raw) > HOOK_REPORT_MAX_LINE_BYTES and not raw.endswith(b"\n")
            rest = raw if oversized else b"\n"
            while rest and not rest.endswith(b"\n"):
                rest = handle.readline(1 << 16)
            if not oversized and not raw.strip():
                continue
            seen += 1
            report = None if oversized or seen > HOOK_REPORT_MAX_LINES else hook_report(raw)
            if report is None:
                dropped += 1
            else:
                reports.append(report)
    return reports, dropped


def is_protected(unit_dir: Path) -> bool:
    """Whether the unit sets prevent_destroy, which makes Terragrunt refuse to plan its destruction."""
    rendered = subprocess.run(["terragrunt", "render", "--json", "--non-interactive"], cwd=unit_dir,
                              capture_output=True, text=True, check=False)
    try:
        return rendered.returncode == 0 and json.loads(rendered.stdout).get("prevent_destroy") is True
    except json.JSONDecodeError:
        return False


def destroy_unit(root: Path, unit: str, expect: dict, engine: str, sink: object = None,
                 log_lines: bool = False) -> dict:
    """Plan one unit's destruction, check it, then destroy: applying the saved plan would skip `destroy` hooks."""
    unit_dir = root / unit
    collector = ErrorExcerptCollector() if reporting(sink) and log_lines else None
    if streamed(unit, unit_dir, "plan", "-destroy", "-input=false", "-out=tfplan-destroy", sink=sink,
                log_lines=log_lines, collector=collector) != 0:
        if is_protected(unit_dir):
            return {"unit": unit, "outcome": "protected", "reason": "protected by prevent_destroy"}
        return {"unit": unit, "outcome": "plan failed", **excerpt_row(collector, sink)}
    plan_file = find_newest(unit_dir, "tfplan-destroy")
    if plan_file is None:
        return {"unit": unit, "outcome": "left no plan"}
    shown = subprocess.run([engine, "show", "-json", plan_file.name], cwd=plan_file.parent,
                           capture_output=True, text=True, check=True)
    row = {"unit": unit, **counts_of(resources(json.loads(shown.stdout)))}
    violation = destroy_violation(row, expect)
    if violation:
        return {**row, "outcome": "refused", "reason": violation}
    if not any(row[key] for key in COUNT_KEYS):
        return {**row, "outcome": "unchanged"}
    collector = ErrorExcerptCollector() if reporting(sink) and log_lines else None
    report_file = hook_report_path() if reporting(sink) else None
    try:
        code = streamed(unit, unit_dir, "destroy", "-auto-approve", "-input=false", sink=sink, log_lines=log_lines,
                        collector=collector, env={HOOK_REPORT_VARIABLE: str(report_file)} if report_file else None)
    finally:
        if report_file is not None:
            forward_hook_reports(unit, report_file, sink)
    if code != 0:
        return {**row, "outcome": "destroy failed", **excerpt_row(collector, sink)}
    return {**row, "outcome": "destroyed"}


def forward_hook_reports(unit: str, path: Path, sink: object) -> None:
    """Each valid report as teardown.hook.reported; never raises, and the file is gone afterwards."""
    try:
        reports, dropped = read_hook_reports(path)
        for report in reports:
            emit(sink, "teardown.hook.reported", {"unit": unit, **report})
        if dropped:
            with _PRINT:
                print(f"::warning title=Teardown hook reports::{unit}: dropped {dropped} report lines that were "
                      "malformed or over the caps", flush=True)
    except Exception:
        pass
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


DESTROY_FAILURES = {"plan failed": ("plan_failed", "plan_failed"), "left no plan": ("plan_failed", "left_no_plan"),
                    "refused": ("expectation_violated", "refused"), "destroy failed": ("destroy_failed", "destroy_failed")}


def destroy_unit_reported(root: Path, unit: str, expect: dict, engine: str, sink: object = None,
                          log_lines: bool = False) -> dict:
    """destroy_unit, reporting the unit's start and end from its own result row; the row keeps its spaced outcome."""
    emit(sink, "terragrunt.unit.started", {"unit": unit})
    try:
        row = destroy_unit(root, unit, expect, engine, sink, log_lines)
    except Exception:
        emit(sink, "terragrunt.unit.failed", {"unit": unit, "reason": "plan_failed", "outcome": "plan_failed"})
        raise
    excerpt = row.pop("excerpt", None)
    if row["outcome"] in ("destroyed", "unchanged", "protected"):
        emit(sink, "terragrunt.unit.completed", {"unit": unit, "destroy": row.get("destroy", 0),
                                                 "outcome": row["outcome"]})
    else:
        reason, outcome = DESTROY_FAILURES.get(row["outcome"], ("plan_failed", "plan_failed"))
        failed = {"unit": unit, "reason": reason, "outcome": outcome}
        if isinstance(excerpt, dict):
            failed |= {"error_excerpt": excerpt["text"], "error_excerpt_shortened": excerpt["shortened"]}
        emit(sink, "terragrunt.unit.failed", failed)
    return row


PHASES = {"fresh": (fresh_unit, "applied", "apply"), "destroy": (destroy_unit_reported, "destroyed", "destroy")}


def dependencies_of(unit: str, deps: dict[str, set[str]]) -> set[str]:
    found: set[str] = set()
    pending = list(deps.get(unit, ()))
    while pending:
        dep = pending.pop()
        if dep not in found:
            found.add(dep)
            pending.extend(deps.get(dep, ()))
    return found


APPLY_PHASE = "Apply the stack"
DESTROY_PHASE = "Destroy the stack"
SETTLED = {"applied", "destroyed", "unchanged", "protected", "kept"}


def run_levels(phase: str, root: Path, order: list[str], expect: dict, engine: str, parallelism: int,
               sink: object = None, log_lines: bool = False) -> list[dict]:
    """Level by level, dependencies first for fresh and last for destroy; a level that fails anywhere is the last one run.

    Like `run --all destroy`, a destroy keeps every prevent_destroy unit and everything it depends on.
    """
    check_policy(expect)
    if "max_monthly_cost_delta" in expect:
        raise Refused("max_monthly_cost_delta needs saved plans with a cost estimate; the fresh and destroy phases have none")
    worker = PHASES[phase][0]
    report = reporting(sink)
    name = DESTROY_PHASE if phase == "destroy" else APPLY_PHASE
    started = time.monotonic()
    if report:
        emit(sink, "run.phase.started", {"phase": name})
        emit(sink, "run.units.seeded", {"units": dag_units(root, order, "destroy" if phase == "destroy" else "")})
    listing = subprocess.run(["terragrunt", "list", "--long", "--dependencies", "--queue-construct-as", PHASES[phase][2]],
                             cwd=root.resolve(), capture_output=True, text=True, check=True).stdout
    deps = parse_dependencies(listing, order)
    groups = levels(order, deps)
    rows: list[dict] = []
    kept: dict[str, str] = {}
    for group in reversed(groups) if phase == "destroy" else groups:
        for unit in (unit for unit in group if unit in kept):
            rows.append({"unit": unit, "outcome": "kept", "reason": f"a dependency of protected {kept[unit]}"})
            if report:
                emit(sink, "terragrunt.unit.completed", {"unit": unit, "outcome": "kept", "kept_by": kept[unit]})
        todo = [unit for unit in group if unit not in kept]
        with ThreadPoolExecutor(max_workers=max(1, parallelism)) as pool:
            done = list(pool.map(lambda unit: worker(root, unit, expect, engine, sink=sink, log_lines=log_lines) if report
                                 else worker(root, unit, expect, engine), todo))
        rows.extend(done)
        for row in done:
            if row["outcome"] == "protected":
                for dep in dependencies_of(row["unit"], deps):
                    kept.setdefault(dep, row["unit"])
        if any(row["outcome"] not in SETTLED for row in done):
            break
    else:
        if report:
            emit(sink, "run.phase.completed", {"phase": name, "elapsed_seconds": round(time.monotonic() - started)})
    return rows


def levels_report(phase: str, rows: list[dict], order: list[str]) -> str:
    title = {"fresh": "Fresh apply", "destroy": "Destroy"}[phase]
    lines = [f"### {title}", "", "| Unit | Add | Change | Replace | Destroy | Outputs | Outcome |",
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
    plans.add_argument("--second-approver-cost", default="", help="Monthly cost delta above which two approve")
    plans.add_argument("--comments-file", default="", help="The gate issue's comments, as the GitHub API lists them")
    plans.add_argument("--approvers", default="")
    run = sub.add_parser("run")
    run.add_argument("--phase", choices=["check", "apply", "clean", "fresh", "destroy"], required=True)
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
        if args.command == "run" and args.phase in PHASES:
            order = [line.strip() for line in args.units.splitlines() if line.strip()]
            expect = json.loads(args.expect_json or "{}")
            sink = build_sink()
            try:
                rows = run_levels(args.phase, Path(args.working_directory), order, expect, args.engine,
                                  args.parallelism, sink=sink, log_lines=log_lines_wanted())
            finally:
                if sink is not None:
                    sink.close()
                    if callable(getattr(sink, "summary", None)):
                        print(f"::notice title=console-events::{sink.summary()}", flush=True)
            report = levels_report(args.phase, rows, order)
            print(report, end="")
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
                    out.write(report)
            outcome = PHASES[args.phase][1]
            applied = [row["unit"] for row in rows if row["outcome"] == outcome]
            write_outputs(changes=json.dumps(rows, separators=(",", ":")))
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
                    out.write(f"{outcome}<<APPLY_UNITS_EOF\n" + "".join(u + "\n" for u in applied) + "APPLY_UNITS_EOF\n")
            stopped = [row for row in rows if row["outcome"] not in SETTLED]
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
            elif args.second_approver_cost.strip():
                why = second_approver_reason(metadata, float(args.second_approver_cost))
                comments = json.loads(Path(args.comments_file).read_text(encoding="utf-8")) if args.comments_file else []
                seen = approvals(comments, marker, approvers_of(args.approvers))
                if why and len(seen) < 2:
                    # Not a refusal: the issue stays pending until a second approver comments.
                    who = ", ".join("@" + login for login in seen) or "nobody yet"
                    reason = f"this apply needs two approvers because {why}; approved so far by {who}"
                    print(f"::notice title=Waiting for a second approver::{reason}")
                    write_outputs(waiting="true", reason=reason, has_changes="false",
                                  destroy=str(bool(metadata.get("destroy"))).lower())
                    return 0
                if why:
                    print(f"Two approvers needed and given: {', '.join('@' + login for login in seen)}")
            units = [u["unit"] for u in metadata["units"] if changing(u)]
            verb = "destroy" if metadata.get("destroy") else "apply"
            print(f"{len(units)} units to {verb}: {', '.join(units) or 'none'}")
            write_outputs(has_changes=str(bool(units)).lower(), destroy=str(bool(metadata.get("destroy"))).lower())
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
                    out.write("units<<APPLY_UNITS_EOF\n" + "".join(u + "\n" for u in units) + "APPLY_UNITS_EOF\n")
            return 0
        wanted = {line.strip() for line in args.units.splitlines() if line.strip()}
        units = [u for u in metadata["units"] if changing(u) and (not wanted or u["unit"] in wanted)]
        root = Path(args.working_directory)
        if args.phase == "check":
            run_check(root, units, args.engine, bool(metadata.get("destroy")))
        elif args.phase == "apply" and metadata.get("destroy"):
            run_destroy(root, units)
        elif args.phase == "apply":
            run_apply(root, units, Path(args.plans_dir))
        else:
            run_clean(root, units)
        return 0
    except Refused as reason:
        return refuse(str(reason))


if __name__ == "__main__":
    sys.exit(main())
