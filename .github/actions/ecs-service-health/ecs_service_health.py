#!/usr/bin/env python3
"""Post the facts of every ECS service in an environment's cluster to the Runestra Console.

Best effort: every path exits 0, and a check that cannot read the services posts a reason instead
of failing the step. It reads what assert-ecs reads (services, deployments, target health, the
Route 53 aliases and the 301-then-2xx answer of each) and posts raw facts, never a verdict, through
the console-events EventSink; mode diagnostics asks ECS why services did not settle. The cluster
name comes from the ecs unit's state object; the CLI's output and the state body are never
printed. It lives beside its action because the public mirror copies actions only.
→ docs/ecs-service-health.md

    MODE=snapshot ECS_STACK_PATH=terragrunt/dev/eu-central-1/ecs AWS_REGION=eu-central-1 \\
        python3 .github/actions/ecs-service-health/ecs_service_health.py
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

AWS_TIMEOUT = 120
DESCRIBE_SERVICES_BATCH = 10
DESCRIBE_TARGET_GROUPS_BATCH = 20
LIST_PAGE_ITEMS = 100
MAX_LIST_PAGES = 50
MAX_DEPLOYMENTS = 5
MAX_TARGET_GROUPS = 10
MAX_PROBES = 20
ECS_DIAGNOSTIC_SERVICES = 10
ECS_DIAGNOSTIC_EVENTS = 20
ECS_DIAGNOSTIC_STOPPED_TASKS = 5
ECS_DIAGNOSTIC_TEXT_MAX = 400
EVENT_VERSION = 1
SETTLE_INTERVAL = 15
MAX_SETTLE_SECONDS = 1800
PROBE_TIMEOUT = 10
PROBE_WORKERS = 8
MAX_REDIRECTS = 5
MAX_CONTAINERS = 5
PAYLOAD_BUDGET = 15 * 1024
EVENTS_BUDGET = 9728
TASKS_BUDGET = 5 * 1024
USER_AGENT = "runestra-ecs-service-health"
STATE_KEY_SUFFIX = "/.terragrunt-stack/ecs/.terragrunt-stack/ecs/terragrunt.tfstate"
TITLE = "ECS service health"
MODES = ("snapshot", "diagnostics")

_sleep = time.sleep
_monotonic = time.monotonic
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_ZONE_RE = re.compile(r"""^domainZone:[ \t]*(?:"([^"]*)"|'([^']*)'|([^\s#"']*))""", re.M)


class AwsError(Exception):
    """A read the CLI refused or could not make; `reason` is the closed code the Console reads."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def warn(reason: str) -> None:
    print(f"::warning title={TITLE}::{reason}", flush=True)


def notice(reason: str) -> None:
    print(f"::notice title={TITLE}::{reason}", flush=True)


def summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError:
        pass


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(text: object, limit: int = ECS_DIAGNOSTIC_TEXT_MAX) -> str | None:
    """ECS or ELB text as one plain line, ANSI and control characters removed, at most `limit` characters."""
    if not isinstance(text, str):
        return None
    flat = _CONTROL_RE.sub(" ", _ANSI_RE.sub("", text)).strip()
    return flat[:limit] if flat else None


def count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def aws(args: list[str]) -> subprocess.CompletedProcess | None:
    """Run one aws CLI call with its output captured; None when the CLI did not run."""
    try:
        return subprocess.run(
            ["aws", *args, "--output", "json"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=AWS_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def classify(stderr: str) -> str:
    if "ClusterNotFoundException" in stderr:
        return "cluster_not_found"
    if "AccessDenied" in stderr:
        return "access_denied"
    return "aws_error"


def call(args: list[str]) -> object:
    """The parsed JSON answer of one call; AwsError with the classified reason otherwise."""
    result = aws(args)
    if result is None:
        raise AwsError("aws_error")
    if result.returncode != 0:
        raise AwsError(classify(result.stderr or ""))
    text = result.stdout or ""
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError:
        raise AwsError("aws_error") from None


def account_id(region: str) -> str | None:
    try:
        answer = call(["sts", "get-caller-identity", "--region", region])
    except AwsError:
        return None
    value = answer.get("Account") if isinstance(answer, dict) else None
    return value if isinstance(value, str) and value else None


def state_prefix(ecs_stack_path: str) -> str:
    """helpers/aws.hcl keys state as terragrunt/<path from repo root>/terragrunt.tfstate."""
    return f"terragrunt/{ecs_stack_path.strip('/')}/"


def cluster_from_state(account: str, region: str, ecs_stack_path: str) -> str | None:
    """The ecs unit's cluster_name output, read from its state object in memory; None when unreadable."""
    bucket = f"{account}-{region}-terraform-states"
    try:
        listing = call(["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", state_prefix(ecs_stack_path),
                        "--region", region])
    except AwsError:
        return None
    contents = listing.get("Contents") if isinstance(listing, dict) else None
    keys = [item.get("Key") for item in contents or [] if isinstance(item, dict)]
    wanted = [key for key in keys if isinstance(key, str) and key.endswith(STATE_KEY_SUFFIX)]
    if len(wanted) != 1:
        return None
    try:
        body = call(["s3", "cp", f"s3://{bucket}/{wanted[0]}", "-", "--region", region])
    except AwsError:
        return None
    outputs = body.get("outputs") if isinstance(body, dict) else None
    entry = outputs.get("cluster_name") if isinstance(outputs, dict) else None
    value = entry.get("value") if isinstance(entry, dict) else None
    return value if isinstance(value, str) and value else None


def check_cluster(cluster: str, region: str) -> None:
    """AwsError("cluster_not_found") for a missing or INACTIVE cluster."""
    answer = call(["ecs", "describe-clusters", "--clusters", cluster, "--region", region])
    clusters = answer.get("clusters") if isinstance(answer, dict) else None
    statuses = [item.get("status") for item in clusters or [] if isinstance(item, dict)]
    if not statuses or statuses[0] != "ACTIVE":
        raise AwsError("cluster_not_found")


def list_service_arns(cluster: str, region: str) -> list[str]:
    arns: list[str] = []
    token = ""
    for _ in range(MAX_LIST_PAGES):
        args = ["ecs", "list-services", "--cluster", cluster, "--region", region,
                "--max-items", str(LIST_PAGE_ITEMS)]
        if token:
            args += ["--starting-token", token]
        page = call(args)
        page = page if isinstance(page, dict) else {}
        arns += [arn for arn in page.get("serviceArns") or [] if isinstance(arn, str)]
        token = page.get("NextToken") or ""
        if not token:
            break
    return arns


def describe_services(cluster: str, region: str, arns: list[str]) -> list[dict]:
    """describe-services takes at most ten services per call."""
    services: list[dict] = []
    for start in range(0, len(arns), DESCRIBE_SERVICES_BATCH):
        chunk = arns[start:start + DESCRIBE_SERVICES_BATCH]
        answer = call(["ecs", "describe-services", "--cluster", cluster, "--region", region, "--services", *chunk])
        found = answer.get("services") if isinstance(answer, dict) else None
        services += [item for item in found or [] if isinstance(item, dict)]
    return services


def group_name_from_arn(arn: str) -> str:
    parts = arn.split(":targetgroup/", 1)
    return parts[1].split("/", 1)[0] if len(parts) == 2 else arn


def group_names(region: str, arns: list[str]) -> dict[str, str]:
    """Target group names by ARN; the name inside the ARN stands in when the call is refused."""
    names = {arn: group_name_from_arn(arn) for arn in arns}
    for start in range(0, len(arns), DESCRIBE_TARGET_GROUPS_BATCH):
        chunk = arns[start:start + DESCRIBE_TARGET_GROUPS_BATCH]
        try:
            answer = call(["elbv2", "describe-target-groups", "--region", region, "--target-group-arns", *chunk])
        except AwsError:
            continue
        for group in (answer.get("TargetGroups") if isinstance(answer, dict) else None) or []:
            if isinstance(group, dict) and group.get("TargetGroupArn") in names and group.get("TargetGroupName"):
                names[group["TargetGroupArn"]] = str(group["TargetGroupName"])
    return names


def service_groups(service: dict) -> list[str]:
    arns = {lb.get("targetGroupArn") for lb in service.get("loadBalancers") or [] if isinstance(lb, dict)}
    return sorted(arn for arn in arns if isinstance(arn, str) and arn)


def target_health(region: str, arns: list[str]) -> dict[str, dict]:
    """{arn: {"name", "arn", "targets"}} with one describe-target-health per group."""
    names = group_names(region, arns)
    groups: dict[str, dict] = {}
    for arn in arns:
        answer = call(["elbv2", "describe-target-health", "--region", region, "--target-group-arn", arn])
        found = answer.get("TargetHealthDescriptions") if isinstance(answer, dict) else None
        groups[arn] = {"name": names[arn], "arn": arn, "targets": [t for t in found or [] if isinstance(t, dict)]}
    return groups


def target_id(description: dict) -> str:
    target = description.get("Target") or {}
    ident = str(target.get("Id", ""))
    port = target.get("Port")
    return f"{ident}:{port}" if port is not None else ident


def target_state(description: dict) -> str:
    state = (description.get("TargetHealth") or {}).get("State")
    return state if isinstance(state, str) and state else "unknown"


def observed_payload(service: dict, groups: list[dict], snapshot_id: str) -> dict:
    """One service's raw facts in the version-1 shape; lists capped, texts clamped, no verdict."""
    groups = sorted(groups, key=lambda group: group["name"])[:MAX_TARGET_GROUPS]
    problems = []
    for group in groups:
        for description in group["targets"]:
            if target_state(description) != "healthy":
                problems.append((target_id(description), group["name"], description))
    first_problem = None
    if problems:
        ident, name, description = min(problems, key=lambda item: (item[0], item[1]))
        first_problem = {
            "id": clean(ident, 255), "group": clean(name, 255), "state": clean(target_state(description), 64),
            "reason": clean((description.get("TargetHealth") or {}).get("Reason")),
        }
    deployments = [d for d in service.get("deployments") or [] if isinstance(d, dict)][:MAX_DEPLOYMENTS]
    return {
        "version": EVENT_VERSION,
        "snapshot_id": snapshot_id,
        "name": clean(service.get("serviceName"), 255),
        "status": clean(service.get("status"), 64),
        "desired": count(service.get("desiredCount")),
        "running": count(service.get("runningCount")),
        "pending": count(service.get("pendingCount")),
        "deployments": [{"status": clean(d.get("status"), 64), "rollout_state": clean(d.get("rolloutState"), 64)}
                        for d in deployments],
        "target_groups": [{"name": clean(group["name"], 255),
                           "healthy": sum(1 for t in group["targets"] if target_state(t) == "healthy"),
                           "total": len(group["targets"])} for group in groups],
        "first_problem": first_problem,
    }


def read_services(cluster: str, region: str) -> list[dict]:
    """Every service with its target groups, in name order; AwsError when any read fails."""
    check_cluster(cluster, region)
    arns = list_service_arns(cluster, region)
    if not arns:
        return []
    services = describe_services(cluster, region, arns)
    group_arns = sorted({arn for service in services for arn in service_groups(service)})
    groups = target_health(region, group_arns) if group_arns else {}
    rows = [{"service": service, "groups": [groups[arn] for arn in service_groups(service) if arn in groups]}
            for service in services]
    return sorted(rows, key=lambda row: str(row["service"].get("serviceName", "")))


def settled(service: dict) -> bool:
    """Running as many tasks as desired, with the PRIMARY rollout completed."""
    if count(service.get("runningCount")) != count(service.get("desiredCount")):
        return False
    primary = [d for d in service.get("deployments") or [] if isinstance(d, dict) and d.get("status") == "PRIMARY"]
    return not primary or primary[0].get("rolloutState") in (None, "COMPLETED")


def steady(rows: list[dict]) -> bool:
    """Every service settled and every registered target healthy."""
    return all(settled(row["service"]) and all(target_state(t) == "healthy" for g in row["groups"] for t in g["targets"])
               for row in rows)


def settle(read, deadline_seconds: int, sleep=None, clock=None) -> list[dict]:
    """Poll every SETTLE_INTERVAL seconds until steady or the deadline; a later failed poll keeps the last read."""
    sleep = sleep or _sleep
    clock = clock or _monotonic
    started = clock()
    rows = read()
    while not steady(rows):
        remaining = deadline_seconds - (clock() - started)
        if remaining <= 0:
            break
        sleep(min(SETTLE_INTERVAL, remaining))
        try:
            rows = read()
        except AwsError:
            break
    return rows


def settle_seconds() -> int:
    try:
        value = int(os.environ.get("SETTLE_SECONDS", "").strip() or 0)
    except ValueError:
        return 0
    return max(0, min(value, MAX_SETTLE_SECONDS))


def zone_from_boilerplate(path: Path) -> str | None:
    """The top-level domainZone value of the stack's .boilerplate.yml, read by line, not by a YAML parser."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = _ZONE_RE.search(text)
    if not match:
        return None
    value = next((group for group in match.groups() if group is not None), "").strip().rstrip(".")
    return value or None


def discover_hosts(zone: str, region: str) -> list[str]:
    """Names of the A alias records in the zone, sorted, at most MAX_PROBES."""
    answer = call(["route53", "list-hosted-zones-by-name", "--dns-name", f"{zone}.", "--region", region])
    zones = [z for z in (answer.get("HostedZones") if isinstance(answer, dict) else None) or []
             if isinstance(z, dict) and str(z.get("Name", "")).lower() == f"{zone}.".lower()]
    zones.sort(key=lambda z: bool((z.get("Config") or {}).get("PrivateZone")))
    if not zones or not zones[0].get("Id"):
        return []
    answer = call(["route53", "list-resource-record-sets", "--hosted-zone-id", str(zones[0]["Id"]),
                   "--region", region])
    records = (answer.get("ResourceRecordSets") if isinstance(answer, dict) else None) or []
    names = {str(r.get("Name", "")).rstrip(".") for r in records
             if isinstance(r, dict) and r.get("Type") == "A" and r.get("AliasTarget")}
    return sorted(name for name in names if name and "\\" not in name)[:MAX_PROBES]


def http_get(host: str) -> tuple[int, str | None]:
    """http://host/ without following: the status and the Location header."""
    connection = http.client.HTTPConnection(host, timeout=PROBE_TIMEOUT)
    try:
        connection.request("GET", "/", headers={"User-Agent": USER_AGENT})
        response = connection.getresponse()
        return response.status, response.getheader("Location")
    finally:
        connection.close()


def https_get(host: str) -> int:
    """https://host/ following at most MAX_REDIRECTS redirects: the last status."""
    url = f"https://{host}/"
    status = 0
    for _ in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme == "https":
            connection = http.client.HTTPSConnection(parts.netloc, timeout=PROBE_TIMEOUT,
                                                     context=ssl.create_default_context())
        elif parts.scheme == "http":
            connection = http.client.HTTPConnection(parts.netloc, timeout=PROBE_TIMEOUT)
        else:
            return status
        try:
            target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            connection.request("GET", target, headers={"User-Agent": USER_AGENT})
            response = connection.getresponse()
            status, location = response.status, response.getheader("Location")
        finally:
            connection.close()
        if not (300 <= status < 400 and location):
            return status
        url = urllib.parse.urljoin(url, location)
    return status


def probe_error(error: BaseException) -> str:
    if isinstance(error, ssl.SSLError):
        return "tls"
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(error, ConnectionRefusedError):
        return "connection_refused"
    return "other"


def probe(host: str, get_http=None, get_https=None) -> dict:
    """The two questions assert-ecs asks: does http redirect, and does https answer."""
    get_http = get_http or http_get
    get_https = get_https or https_get
    result = {"host": host, "http_status": None, "redirect_to": None, "https_status": None, "error": None}
    try:
        status, location = get_http(host)
    except Exception as error:
        result["error"] = probe_error(error)
        return result
    result["http_status"] = status
    result["redirect_to"] = clean(location, 255) if 300 <= status < 400 else None
    try:
        result["https_status"] = get_https(host)
    except Exception as error:
        result["error"] = probe_error(error)
    return result


def endpoints(stack_path: str, region: str) -> dict:
    zone = zone_from_boilerplate(Path(stack_path) / ".boilerplate.yml")
    if zone is None:
        return {"state": "no_zone", "zone": None, "probes": []}
    try:
        hosts = discover_hosts(zone, region)
    except AwsError:
        hosts = []
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        probes = list(pool.map(probe, hosts))
    return {"state": "checked", "zone": clean(zone, 255), "probes": probes}


def payload_size(payload: dict) -> int:
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def event_time(value: object) -> str | None:
    """An ECS timestamp (ISO-8601 or epoch seconds) as UTC ISO-8601 with Z."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            moment = datetime.fromtimestamp(value, tz=timezone.utc)
        elif isinstance(value, str):
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            moment = moment.replace(tzinfo=moment.tzinfo or timezone.utc)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return clean(value, 64)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stopped_tasks(cluster: str, region: str, name: str) -> list[dict]:
    try:
        listed = call(["ecs", "list-tasks", "--cluster", cluster, "--region", region, "--service-name", name,
                       "--desired-status", "STOPPED", "--max-items", str(ECS_DIAGNOSTIC_STOPPED_TASKS)])
        arns = [a for a in (listed.get("taskArns") if isinstance(listed, dict) else None) or [] if isinstance(a, str)]
        if not arns:
            return []
        described = call(["ecs", "describe-tasks", "--cluster", cluster, "--region", region,
                          "--tasks", *arns[:ECS_DIAGNOSTIC_STOPPED_TASKS]])
    except AwsError:
        return []
    tasks = (described.get("tasks") if isinstance(described, dict) else None) or []
    return [task for task in tasks if isinstance(task, dict)][:ECS_DIAGNOSTIC_STOPPED_TASKS]


def diagnosed_payload(service: dict, tasks: list[dict]) -> dict:
    """ECS's own account of one service: newest events and stopped tasks, capped and clamped."""
    events = [{"at": event_time(e.get("createdAt")), "message": clean(e.get("message")) or ""}
              for e in service.get("events") or [] if isinstance(e, dict)]
    events.sort(key=lambda e: e["at"] or "", reverse=True)
    stopped = []
    for task in tasks[:ECS_DIAGNOSTIC_STOPPED_TASKS]:
        containers = [c for c in task.get("containers") or [] if isinstance(c, dict)][:MAX_CONTAINERS]
        stopped.append({
            "task_id": clean(str(task.get("taskArn", "")).rsplit("/", 1)[-1], 64),
            "stopped_reason": clean(task.get("stoppedReason")),
            "containers": [{"name": clean(c.get("name"), 255), "reason": clean(c.get("reason")),
                            "exit_code": c.get("exitCode") if isinstance(c.get("exitCode"), int) else None}
                           for c in containers],
        })
    events = events[:ECS_DIAGNOSTIC_EVENTS]
    while payload_size({"events": events}) > EVENTS_BUDGET:
        events.pop()
    while payload_size({"stopped_tasks": stopped}) > TASKS_BUDGET:
        widest = max(stopped, key=lambda task: len(task["containers"]))
        if len(widest["containers"]) > 1:
            widest["containers"].pop()
        else:
            stopped.pop()
    return {"version": EVENT_VERSION, "name": clean(service.get("serviceName"), 255), "events": events,
            "stopped_tasks": stopped}


def log_group(payload: dict) -> None:
    """One ::group:: block per service for the GitHub log, ECS's text already flattened by clean()."""
    print(f"::group::Service {payload['name']}", flush=True)
    for event in payload["events"]:
        print(f"{event['at'] or '-'} {event['message']}", flush=True)
    for task in payload["stopped_tasks"]:
        print(f"Stopped task {task['task_id']}: {task['stopped_reason'] or '-'}", flush=True)
        for container in task["containers"]:
            print(f"  {container['name']}: exit {container['exit_code']} {container['reason'] or ''}".rstrip(),
                  flush=True)
    print("::endgroup::", flush=True)


def diagnose(sink: object, cluster: str, region: str) -> None:
    """One ecs.service.diagnosed per service, services that did not settle first."""
    try:
        check_cluster(cluster, region)
        services = describe_services(cluster, region, list_service_arns(cluster, region))
    except AwsError as error:
        notice(f"ECS services could not be read for diagnostics: {error.reason}")
        return
    ordered = sorted(services, key=lambda s: (settled(s), str(s.get("serviceName", ""))))
    for service in ordered[:ECS_DIAGNOSTIC_SERVICES]:
        name = str(service.get("serviceName", ""))
        payload = diagnosed_payload(service, stopped_tasks(cluster, region, name) if name else [])
        log_group(payload)
        emit(sink, "ecs.service.diagnosed", payload)
    summary(f"ECS services diagnosed: {min(len(ordered), ECS_DIAGNOSTIC_SERVICES)}")


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
    """Telemetry never fails the step."""
    if sink is None:
        return
    try:
        sink.emit(event_type, payload)
    except Exception:
        pass


@contextmanager
def phase(sink: object, name: str):
    """run.phase.started and run.phase.completed around the body when a phase is named."""
    started = _monotonic()
    if name:
        emit(sink, "run.phase.started", {"phase": name})
    try:
        yield
    finally:
        if name:
            emit(sink, "run.phase.completed", {"phase": name, "elapsed_seconds": round(_monotonic() - started)})


def check_failed(sink: object, cluster: str | None, reason: str, checked_at: str) -> None:
    emit(sink, "ecs.services.check_failed",
         {"version": EVENT_VERSION, "checked_at": checked_at, "cluster": cluster, "reason": reason})
    summary(f"ECS services were not read: {reason}")


def snapshot(sink: object, cluster: str, region: str, stack_path: str = "", wait: int = 0) -> None:
    """One observed event per service, then the closing snapshot; check_failed instead when unreadable."""
    try:
        rows = settle(lambda: read_services(cluster, region), wait)
    except AwsError as error:
        check_failed(sink, cluster, error.reason, utc_now())
        return
    checked_at = utc_now()
    snapshot_id = uuid.uuid4().hex
    closing = {
        "version": EVENT_VERSION, "snapshot_id": snapshot_id, "checked_at": checked_at, "cluster": cluster,
        "service_count": len(rows), "endpoints": endpoints(stack_path, region),
    }
    while payload_size(closing) > PAYLOAD_BUDGET and closing["endpoints"]["probes"]:
        closing["endpoints"]["probes"].pop()
    for row in rows:
        emit(sink, "ecs.service.observed", observed_payload(row["service"], row["groups"], snapshot_id))
    emit(sink, "ecs.services.snapshot", closing)
    summary(f"ECS services observed: {len(rows)}; endpoints probed: {len(closing['endpoints']['probes'])}")


def run(sink: object) -> None:
    mode = os.environ.get("MODE", "").strip() or "snapshot"
    stack_path = os.environ.get("ECS_STACK_PATH", "").strip()
    region = os.environ.get("AWS_REGION", "").strip() or os.environ.get("AWS_DEFAULT_REGION", "").strip()
    is_check = bool(os.environ.get("PHASE", "").strip())
    if mode not in MODES:
        warn(f"Skipped: mode must be one of {', '.join(MODES)}")
        return
    if not stack_path or not region:
        warn("Skipped: no ecs_stack_path or no AWS region for this run")
        return
    account = account_id(region)
    cluster = cluster_from_state(account, region, stack_path) if account else None
    if cluster is None:
        if is_check and mode == "snapshot":
            check_failed(sink, None, "cluster_not_found", utc_now())
        else:
            notice("No ECS cluster name in this environment's state; nothing to check")
        return
    if mode == "diagnostics":
        diagnose(sink, cluster, region)
        return
    snapshot(sink, cluster, region, stack_path, settle_seconds())


def main() -> int:
    sink: object = None
    try:
        sink = build_sink()
        with phase(sink, os.environ.get("PHASE", "").strip()):
            run(sink)
    except Exception:  # noqa: BLE001 - best effort, never fail the step
        warn("ECS services were not checked: unexpected error")
    finally:
        if sink is not None:
            try:
                sink.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
