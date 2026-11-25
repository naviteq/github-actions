#!/usr/bin/env python3
"""Set up an environment's AWS state backend with the catalog's own script and report what it found.

It fetches the catalog at exactly CATALOG_SHA, runs `scripts/state-backend.sh <working_directory>
--bootstrap-dir <dir>` from the repository root with a machine report file, and posts the run phase
and one state_backend.unit.reported event per report line through the console-events EventSink. The
step fails exactly when the script fails; telemetry never changes that. The script's own output
reaches the job log unchanged. It lives beside its action because the public mirror copies actions
only. → docs/state-backend-action.md

    WORKING_DIRECTORY=terragrunt/dev CATALOG_SHA=<40-hex> python3 .github/actions/state-backend/state_backend.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATE_BACKEND_PHASE = "Set up the state backend"
REPORT_VARIABLE = "RUNESTRA_STATE_BACKEND_REPORT_FILE"
REPORT_MAX_LINES = 32
REPORT_MAX_LINE_BYTES = 8192
BUCKET_MAX = 63
OWNER_MAX = 512
EVENT_TYPE = "state_backend.unit.reported"
TITLE = "State backend"
CATALOG_HTTPS = "https://github.com/naviteq-ltd/infrastructure-catalog.git"
CATALOG_SSH = "git@github.com:naviteq-ltd/infrastructure-catalog.git"
SCRIPT_PATH = "scripts/state-backend.sh"
GIT_TIMEOUT = 600
EXIT_FAILED, EXIT_USAGE = 1, 2

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
WORD_RE = re.compile(r"^[a-z][a-z_]{0,63}$")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_USERINFO_RE = re.compile(r"(\w+://)[^@/\s]+@")

_monotonic = time.monotonic


def error(message: str) -> None:
    print(f"::error title={TITLE}::{message}", flush=True)


def warn(message: str) -> None:
    print(f"::warning title={TITLE}::{message}", flush=True)


def summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError:
        pass


def write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(f"{name}={value}\n")
    except OSError:
        pass


def _nullable_str(body: dict, key: str, limit: int | None = None) -> tuple[bool, str | None]:
    value = body.get(key)
    if value is None:
        return True, None
    if not isinstance(value, str) or (limit is not None and len(value) > limit):
        return False, None
    return True, value


def _nullable_count(body: dict, key: str) -> tuple[bool, int | None]:
    value = body.get(key)
    if value is None:
        return True, None
    if type(value) is not int or value < 0:
        return False, None
    return True, value


def report_line(raw: bytes) -> dict | None:
    """One report line as the event payload without `version`; None unless it is exactly the documented shape."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict) or type(body.get("version")) is not int or body["version"] != 1:
        return None
    outcome, code = body.get("outcome"), body.get("code")
    if not isinstance(outcome, str) or not WORD_RE.match(outcome):
        return None
    if code is not None and not (isinstance(code, str) and WORD_RE.match(code)):
        return None
    checks = {
        "unit": _nullable_str(body, "unit"),
        "bucket": _nullable_str(body, "bucket", BUCKET_MAX),
        "owner": _nullable_str(body, "owner", OWNER_MAX),
        "changes": _nullable_count(body, "changes"),
        "state_files": _nullable_count(body, "state_files"),
    }
    if not all(ok for ok, _ in checks.values()):
        return None
    return {"unit": checks["unit"][1], "bucket": checks["bucket"][1], "outcome": outcome, "code": code,
            "owner": checks["owner"][1], "changes": checks["changes"][1], "state_files": checks["state_files"][1]}


def read_reports(path: Path) -> tuple[list[dict], int]:
    """The valid reports in the file and how many lines were dropped; reads at most the capped bytes per line."""
    reports: list[dict] = []
    dropped = seen = 0
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return [], 0
    with handle:
        while raw := handle.readline(REPORT_MAX_LINE_BYTES + 1):
            oversized = len(raw) > REPORT_MAX_LINE_BYTES and not raw.endswith(b"\n")
            rest = raw if oversized else b"\n"
            while rest and not rest.endswith(b"\n"):
                rest = handle.readline(1 << 16)
            if not oversized and not raw.strip():
                continue
            seen += 1
            report = None if oversized or seen > REPORT_MAX_LINES else report_line(raw)
            if report is None:
                dropped += 1
            else:
                reports.append(report)
    return reports, dropped


def summary_line(report: dict) -> str:
    unit = report["unit"] if isinstance(report["unit"], str) and LABEL_RE.match(report["unit"]) else "unit"
    code = f" ({report['code']})" if report["code"] else ""
    return f"- `{unit}`: {report['outcome']}{code}"


def forward_reports(path: Path, sink: object) -> None:
    """Each valid report as one event and one summary line; never raises, and the file is gone afterwards."""
    try:
        reports, dropped = read_reports(path)
        for report in reports:
            emit(sink, EVENT_TYPE, report)
            summary(summary_line(report))
        if dropped:
            warn(f"dropped {dropped} report lines that were malformed or over the caps")
    except Exception:
        pass
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def git(args: list[str]) -> subprocess.CompletedProcess | None:
    """One git call with its output captured and no prompt; None when git did not run."""
    try:
        return subprocess.run(["git", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, check=False, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return None


def catalog_url() -> str:
    """https rides the Console broker's credential helper; without a Console the app-token rewrite covers ssh."""
    return CATALOG_HTTPS if os.environ.get("CONSOLE_URL", "") else CATALOG_SSH


def fetch_catalog(sha: str, dest: Path) -> bool:
    """The catalog at exactly `sha` checked out in `dest`; False, with git's last line printed, when it failed."""
    for args in (["init", "-q", str(dest)], ["-C", str(dest), "fetch", "-q", "--depth=1", catalog_url(), sha],
                 ["-C", str(dest), "checkout", "-q", "FETCH_HEAD"]):
        result = git(args)
        if result is None or result.returncode != 0:
            last = (result.stderr.strip().splitlines() or [""])[-1] if result is not None else "git did not run"
            if last:
                print(_USERINFO_RE.sub(r"\1", last)[:400], flush=True)
            return False
    return True


def run_script(script: Path, working_directory: str, bootstrap: Path, report: Path, logs: Path) -> int:
    """The script's exit status; its stdout and stderr are the step's own."""
    env = {**os.environ, REPORT_VARIABLE: str(report), "TMPDIR": str(logs)}
    sys.stdout.flush()
    try:
        result = subprocess.run(["bash", str(script), working_directory, "--bootstrap-dir", str(bootstrap)],
                                stdin=subprocess.DEVNULL, env=env, check=False)
    except (OSError, subprocess.SubprocessError):
        error("state-backend.sh could not be started")
        return EXIT_FAILED
    return result.returncode if result.returncode >= 0 else EXIT_FAILED


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


def flush(sink: object) -> None:
    """Post what is buffered now, so the Console shows the phase while the script runs."""
    try:
        getattr(sink, "flush", lambda: None)()
    except Exception:
        pass


def set_up(sink: object, temp: Path) -> int:
    """Check the inputs, fetch the catalog, run its script and forward its report; the script's status."""
    working_directory = os.environ.get("WORKING_DIRECTORY", "").strip()
    sha = os.environ.get("CATALOG_SHA", "")
    if os.environ.get("TG_BACKEND_BOOTSTRAP", ""):
        error("TG_BACKEND_BOOTSTRAP is set; Terragrunt would create a plain state bucket, so the step refuses to run")
        return EXIT_FAILED
    if not SHA_RE.match(sha):
        error("catalog_sha must be the catalog commit as 40 lowercase hex characters")
        return EXIT_FAILED
    if not working_directory:
        error("working_directory is empty")
        return EXIT_FAILED
    bootstrap, logs = temp / "state-backend", temp / "state-backend-logs"
    catalog = Path(tempfile.mkdtemp(prefix="state-backend-catalog-", dir=temp))
    report = temp / "state-backend-report.ndjson"
    try:
        bootstrap.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        report.unlink(missing_ok=True)
        if not fetch_catalog(sha, catalog):
            error(f"the catalog commit could not be fetched: {sha}")
            return EXIT_FAILED
        script = catalog / SCRIPT_PATH
        if not script.is_file():
            error(f"the catalog at {sha} has no {SCRIPT_PATH}")
            return EXIT_FAILED
        status = run_script(script, working_directory, bootstrap, report, logs)
        forward_reports(report, sink)
        if status == EXIT_USAGE:
            error("state-backend.sh refused its arguments: this is a workflow bug, not a backend problem")
        return status
    finally:
        shutil.rmtree(catalog, ignore_errors=True)


def main() -> int:
    started = _monotonic()
    temp = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    write_output("bootstrap_dir", str(temp / "state-backend"))
    try:
        sink = build_sink()
    except Exception:
        sink = NullSink()
    emit(sink, "run.phase.started", {"phase": STATE_BACKEND_PHASE})
    flush(sink)
    summary(f"### {STATE_BACKEND_PHASE}")
    status = EXIT_FAILED
    try:
        status = set_up(sink, temp)
    except Exception:  # noqa: BLE001 - an unexpected error fails the step it could not finish
        error("the state backend step stopped on an unexpected error")
    elapsed = {"phase": STATE_BACKEND_PHASE, "elapsed_seconds": round(_monotonic() - started)}
    emit(sink, "run.phase.completed" if status == 0 else "run.phase.failed", elapsed)
    if status != 0:
        summary(f"The step failed with exit status {status}.")
    try:
        sink.close()
    except Exception:
        pass
    return status


if __name__ == "__main__":
    sys.exit(main())
