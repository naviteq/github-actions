#!/usr/bin/env python3
"""Post what an environment's OCI compartment still holds after a destroy to the Runestra Console.

Best effort: every path exits 0, and a listing that could not be made posts a reason instead of
failing the step. The compartment the environment created is resolved by name among the parent's
ACTIVE children, in Python; OCI Search lists it per region until it is empty or the time is up.
Gone items are dropped, items pending deletion (a vault and its keys) are accepted residue counted
in the step summary, and the rest is posted with its OCID as one teardown.hook.reported event
through the console-events EventSink. The CLI's output is never printed. It lives beside its
action because the public mirror copies actions only.
→ docs/compartment-report-oci.md

    COMPARTMENT_NAME=acme-dev PARENT_COMPARTMENT_OCID=ocid1.tenancy.oc1..aaaa \\
        REGIONS="eu-frankfurt-1 us-ashburn-1" python3 .github/actions/compartment-report-oci/compartment_report_oci.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

TITLE = "Compartment report"
EVENT_TYPE = "teardown.hook.reported"
UNIT = "compartment-sweep"
HOOK = "compartment_sweep"
OCI_TIMEOUT = 120
ATTEMPTS = 3
RETRY_SECONDS = 5
SEARCH_PAGE_ITEMS = 1000
MAX_SEARCH_PAGES = 5
MAX_ITEMS = 200
PAYLOAD_BUDGET = 15 * 1024
DEFAULT_TIMEOUT = 300
DEFAULT_INTERVAL = 30
MAX_TIMEOUT = 1800
MAX_INTERVAL = 300

GONE = frozenset({"DELETED", "TERMINATED"})
ACCEPTED_RESIDUE = frozenset({"PENDING_DELETION", "SCHEDULING_DELETION"})
DEFAULT_KIND = "oci_resource"
# OCI Search resource types, as the search answers them; anything else is oci_resource.
KIND_BY_TYPE = {
    "NetworkLoadBalancer": "network_load_balancer",
    "Instance": "compute_instance",
    "LoadBalancer": "load_balancer",
    "Vcn": "vcn_resource",
    "Subnet": "vcn_resource",
    "RouteTable": "vcn_resource",
    "SecurityList": "vcn_resource",
    "InternetGateway": "vcn_resource",
    "NatGateway": "vcn_resource",
    "ServiceGateway": "vcn_resource",
    "DhcpOptions": "vcn_resource",
    "NetworkSecurityGroup": "vcn_resource",
    "PrivateIp": "vcn_resource",
    "Vnic": "vcn_resource",
}

COMPARTMENT_OCID_RE = re.compile(r"ocid1\.compartment\.oc[0-9]+\.[a-z0-9-]*\.[a-z0-9]+")
PARENT_OCID_RE = re.compile(r"ocid1\.(?:compartment|tenancy)\.oc[0-9]+\.[a-z0-9-]*\.[a-z0-9]+")
REGION_RE = re.compile(r"[a-z]{2,}(?:-[a-z0-9]+)+")
ERROR_CODE_RE = re.compile(r'"code":\s*"([A-Za-z]+)"')
ERROR_STATUS_RE = re.compile(r'"status":\s*([0-9]{3})')

_sleep = time.sleep
_monotonic = time.monotonic


class ListingFailed(Exception):
    """A listing the CLI refused or could not make; the report then reads `other`."""


def warn(reason: str) -> None:
    print(f"::warning title={TITLE}::{reason}", flush=True)


def summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError:
        pass


def seconds(name: str, default: int, ceiling: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return min(max(value, 0), ceiling)


def regions_of(text: str) -> list[str] | None:
    """The regions in order, each once; None when one of them is not a region name."""
    regions: list[str] = []
    for region in text.split():
        if not REGION_RE.fullmatch(region):
            return None
        if region not in regions:
            regions.append(region)
    return regions


def error_class(stderr: str) -> str:
    """The service error code and status only; nothing else of the CLI's output is repeated."""
    code = ERROR_CODE_RE.search(stderr or "")
    status = ERROR_STATUS_RE.search(stderr or "")
    if code and status:
        return f"{code.group(1)} ({status.group(1)})"
    return code.group(1) if code else "no service error code"


def oci(args: list[str]) -> object:
    """The parsed answer of one oci call, retried; ListingFailed when no attempt answered."""
    failure = "the oci CLI did not run"
    for attempt in range(ATTEMPTS):
        if attempt:
            # Paced by interval_seconds too, so a caller that polls fast retries fast.
            _sleep(min(RETRY_SECONDS, seconds("INTERVAL_SECONDS", DEFAULT_INTERVAL, MAX_INTERVAL)))
        try:
            result = subprocess.run(["oci", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=OCI_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError, ValueError):
            failure = "the oci CLI did not run"
            continue
        if result.returncode != 0:
            failure = error_class(result.stderr)
            continue
        text = (result.stdout or "").lstrip()
        if not text:
            return {}
        try:
            value, _ = json.JSONDecoder().raw_decode(text)
        except ValueError:
            failure = "an answer that is not JSON"
            continue
        if isinstance(value, dict):
            return value
        failure = "an answer that is not JSON"
    raise ListingFailed(failure)


def matching_compartment(children: object, name: str) -> str | None:
    """The id of the one ACTIVE child named exactly `name`; None when there is none."""
    matches = [
        child for child in children if isinstance(child, dict)
        and child.get("name") == name and str(child.get("lifecycle-state", "")).upper() == "ACTIVE"
    ] if isinstance(children, list) else []
    if len(matches) > 1:
        raise ListingFailed("several active compartments carry the name")
    return str(matches[0].get("id") or "") if matches else None


def resolve(name: str, parent: str) -> str | None:
    """The environment's own compartment under `parent`, matched by name here and never in a query."""
    answer = oci(["iam", "compartment", "list", "--compartment-id", parent, "--all", "--lifecycle-state", "ACTIVE"])
    ocid = matching_compartment(answer.get("data", []) if isinstance(answer, dict) else [], name)
    if ocid is not None and not COMPARTMENT_OCID_RE.fullmatch(ocid):
        raise ListingFailed("the listed id is not a compartment OCID")
    return ocid


def search(region: str, ocid: str) -> list[dict]:
    """Every item OCI Search holds for the compartment in one region, following pages."""
    query = f"query all resources where compartmentId = '{ocid}'"
    items: list[dict] = []
    page = ""
    for _ in range(MAX_SEARCH_PAGES):
        args = ["search", "resource", "structured-search", "--query-text", query,
                "--limit", str(SEARCH_PAGE_ITEMS), "--region", region]
        answer = oci([*args, "--page", page] if page else args)
        data = answer.get("data", {}) if isinstance(answer, dict) else {}
        found = data.get("items", []) if isinstance(data, dict) else []
        items += [entry for entry in found or [] if isinstance(entry, dict)]
        page = str(answer.get("opc-next-page") or "")
        if not page:
            break
    return items


def listing(regions: list[str], ocid: str) -> list[dict]:
    """The items of every region, each identifier once."""
    seen: set[str] = set()
    merged: list[dict] = []
    for region in regions:
        try:
            found = search(region, ocid)
        except ListingFailed as error:
            raise ListingFailed(f"{region}: {error}") from None
        for entry in found:
            key = str(entry.get("identifier") or "") or json.dumps(entry, sort_keys=True)
            if key not in seen:
                seen.add(key)
                merged.append(entry)
    return merged


def classify(items: list[dict]) -> tuple[list[dict], Counter, Counter]:
    """(remaining {kind, id} items, remaining by type and state, accepted residue by type and state)."""
    remaining: list[dict] = []
    left: Counter = Counter()
    residue: Counter = Counter()
    for entry in items:
        kind = str(entry.get("resource-type") or "?")
        state = str(entry.get("lifecycle-state") or "").upper()
        if state in GONE:
            continue
        if state in ACCEPTED_RESIDUE:
            residue[(kind, state)] += 1
            continue
        left[(kind, state or "UNKNOWN")] += 1
        remaining.append({"kind": KIND_BY_TYPE.get(kind, DEFAULT_KIND), "id": str(entry.get("identifier") or "")})
    remaining.sort(key=lambda entry: (entry["kind"], entry["id"]))
    return remaining, left, residue


def payload_of(remaining: list[dict], skipped_reason: str | None) -> dict:
    """The event payload: at most MAX_ITEMS items, cut further until it fits one event line."""
    kept = remaining[:MAX_ITEMS]
    payload = {"unit": UNIT, "hook": HOOK, "removed": [], "remaining": kept, "skipped_reason": skipped_reason}
    if len(kept) < len(remaining):
        payload["truncated"] = True
    while len(kept) > 1 and len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > PAYLOAD_BUDGET:
        kept = kept[:-1]
        payload = {**payload, "remaining": kept, "truncated": True}
    return payload


def counted(title: str, counts: Counter) -> None:
    if not counts:
        return
    summary(title)
    for (kind, state), count in sorted(counts.items()):
        summary(f"- {kind} {state}: {count}")


def sweep(regions: list[str], ocid: str) -> tuple[list[dict], Counter, Counter]:
    """List until nothing remains or the time is up; the last listing is what counts."""
    timeout = seconds("TIMEOUT_SECONDS", DEFAULT_TIMEOUT, MAX_TIMEOUT)
    interval = seconds("INTERVAL_SECONDS", DEFAULT_INTERVAL, MAX_INTERVAL)
    deadline = _monotonic() + timeout
    while True:
        remaining, left, residue = classify(listing(regions, ocid))
        if not remaining or _monotonic() >= deadline:
            return remaining, left, residue
        _sleep(interval)


def run(sink: object) -> None:
    summary(f"### {TITLE}")
    if os.environ.get("COMPARTMENT_OCID", "").strip():
        summary("The environment applies into a compartment it was given, not one it created; nothing was listed.")
        emit(sink, payload_of([], "compartment_not_owned"))
        return
    name = os.environ.get("COMPARTMENT_NAME", "")
    parent = os.environ.get("PARENT_COMPARTMENT_OCID", "").strip()
    regions = regions_of(os.environ.get("REGIONS", ""))
    if not name or not PARENT_OCID_RE.fullmatch(parent) or not regions:
        not_listed(sink, "no compartment_name, no valid parent_compartment_ocid or no valid regions")
        return
    try:
        ocid = resolve(name, parent)
        if ocid is None:
            summary("No active compartment by that name under the parent, so it holds nothing.")
            emit(sink, payload_of([], None))
            return
        remaining, left, residue = sweep(regions, ocid)
    except ListingFailed as error:
        not_listed(sink, f"the compartment could not be listed ({error})")
        return
    if remaining:
        warn(f"{len(remaining)} resources outlived the destroy; the step summary counts them by type")
        counted(f"{len(remaining)} resources outlived the destroy:", left)
    else:
        summary("The environment compartment holds nothing that outlived the destroy.")
    counted("Accepted as residue, pending deletion:", residue)
    emit(sink, payload_of(remaining, None))


def not_listed(sink: object, reason: str) -> None:
    warn(f"Not reported: {reason}")
    summary("The compartment was not listed; the Console reads this report as not reported.")
    emit(sink, payload_of([], "other"))


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


_POSTED: list[bool] = []


def emit(sink: object, payload: dict) -> None:
    """The one report of the run; telemetry never fails the step."""
    if _POSTED:
        return
    _POSTED.append(True)
    if sink is None:
        return
    try:
        sink.emit(EVENT_TYPE, payload)
    except Exception:
        pass


def main() -> int:
    _POSTED.clear()
    sink: object = None
    try:
        sink = build_sink()
        run(sink)
    except Exception:  # noqa: BLE001 - best effort, never fail the step
        warn("The compartment was not reported: unexpected error")
        emit(sink, payload_of([], "other"))
    finally:
        if sink is not None:
            try:
                sink.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
