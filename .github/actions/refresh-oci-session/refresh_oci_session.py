#!/usr/bin/env python3
"""Keep a job's OCI session alive past the one-hour life of its session token.

`once` re-exchanges a fresh GitHub OIDC token for an OCI session token bound to the SAME
ephemeral public key `configure-oci-credentials` generated, and swaps only the token file,
atomically. `start` runs `once` every `interval_seconds` from a detached child; `report`
prints what the child and `once` logged. Stdlib only; it lives beside its action because
the public mirror copies `.github/actions` and nothing else. No client secret, GitHub token,
session token or response body is ever printed or logged. See docs/refresh-oci-session.md.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_INTERVAL_SECONDS = 1200
MIN_INTERVAL_SECONDS = 60
RETRY_SECONDS = 60
HTTP_TIMEOUT = 30
MAX_BODY = 1 << 20
# A job's default timeout; past it the loop has nothing left to keep alive.
MAX_LOOP_SECONDS = 6 * 3600
# Copied from the Console's oci_constants; plain ERE, global or regional identity-domain host.
DOMAIN_HOST_PATTERN = (
    r"^https://idcs-[a-z0-9]+\."
    r"(identity\.oraclecloud\.com"
    r"|[a-z]+-[a-z]+-idcs-[0-9]+\.identity\.[a-z]+-[a-z]+-[0-9]+\.oci\.oraclecloud\.com)$"
)
TOKEN_FILE = "session_token"
PUBLIC_KEY_FILE = "session_key_public.pem"
LOG_FILE = "refresh.log"
PID_FILE = "refresh.pid"
PREFIX = "refresh-oci-session"
STATUSES = ("refreshed", "refresh_failed", "bad_input")
LOG_LINE = re.compile(
    r"refreshed|refresh_failed status=(?:[0-9]{3}|github_[0-9]{3}|transport|github_transport|error)"
)
EXCHANGE_FIELDS = (
    ("grant_type", "urn:ietf:params:oauth:grant-type:token-exchange"),
    ("requested_token_type", "urn:oci:token-type:oci-upst"),
)

_sleep = time.sleep
_monotonic = time.monotonic
_spawn = subprocess.Popen


class RefreshError(Exception):
    """A closed `status` and `code`; never a credential or a response body."""

    def __init__(self, status: str, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Inputs:
    session_dir: Path
    domain_url: str
    audience: str
    client_id: str
    client_secret: str
    request_url: str
    request_bearer: str
    interval: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102 - a credential must not follow a redirect
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(method: str, url: str, headers: dict[str, str], data: bytes | None) -> tuple[int, bytes]:
    """(status, body) for any HTTP answer; OSError or HTTPException when nothing answered."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, resp.read(MAX_BODY)
    except urllib.error.HTTPError as err:
        try:
            return err.code, err.read(MAX_BODY)
        finally:
            err.close()


def _call(method: str, url: str, headers: dict[str, str], data: bytes | None, prefix: str) -> tuple[int, bytes]:
    try:
        return _http(method, url, headers, data)
    except (OSError, http.client.HTTPException, ValueError):
        raise RefreshError("refresh_failed", f"{prefix}transport") from None


def _emit(line: str) -> None:
    print(f"{PREFIX}: {line}", flush=True)


def _error(reason: str) -> None:
    print(f"::error title={PREFIX}::{reason}", flush=True)


def _mask(value: str) -> None:
    print(f"::add-mask::{value}", flush=True)


def _json_field(body: bytes, field: str) -> str | None:
    try:
        value = json.loads(body).get(field)
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, str) and value else None


def normalise_domain_url(raw: str) -> str:
    """The identity-domain origin, or RefreshError before anything is sent to it."""
    url = raw.strip()
    if "://" not in url:
        url = f"https://{url}"
    url = url.removesuffix("/").removesuffix(":443")
    if not re.fullmatch(DOMAIN_HOST_PATTERN, url):
        raise RefreshError("bad_input", "oidc_domain_url_invalid")
    return url


def _interval(raw: str) -> int:
    try:
        value = int(raw.strip() or DEFAULT_INTERVAL_SECONDS)
    except ValueError:
        raise RefreshError("bad_input", "interval_seconds_invalid") from None
    return max(value, MIN_INTERVAL_SECONDS)


def validate(env: dict[str, str]) -> Inputs:
    session = env.get("OCI_SESSION_DIR", "").strip()
    if not session or not (Path(session) / PUBLIC_KEY_FILE).is_file():
        raise RefreshError("bad_input", "session_dir_missing")
    raw_domain = env.get("OIDC_DOMAIN_URL", "").strip()
    if not raw_domain:
        raise RefreshError("bad_input", "oidc_domain_url_missing")
    domain_url = normalise_domain_url(raw_domain)
    for name in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        if not env.get(name, ""):
            raise RefreshError("bad_input", f"{name.lower()}_missing")
    request_url = env.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_bearer = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not request_url or not request_bearer:
        raise RefreshError("bad_input", "oidc_request_unavailable")
    return Inputs(
        session_dir=Path(session), domain_url=domain_url, audience=raw_domain,
        client_id=env["OIDC_CLIENT_ID"], client_secret=env["OIDC_CLIENT_SECRET"],
        request_url=request_url, request_bearer=request_bearer,
        interval=_interval(env.get("INTERVAL_SECONDS", "")),
    )


def public_key_body(path: Path) -> str:
    """The base64 body alone: OCI refuses a PEM that still carries its header lines."""
    lines = path.read_text().splitlines()
    return "".join(line.strip() for line in lines if not line.startswith("-----"))


def github_token(inputs: Inputs) -> str:
    """A fresh Actions OIDC token for the audience configure-oci-credentials asked for."""
    sep = "&" if "?" in inputs.request_url else "?"
    url = f"{inputs.request_url}{sep}audience={urllib.parse.quote(inputs.audience, safe=':/')}"
    headers = {"Authorization": f"bearer {inputs.request_bearer}", "Accept": "application/json"}
    status, body = _call("GET", url, headers, None, "github_")
    value = _json_field(body, "value") if status == 200 else None
    if value is None:
        raise RefreshError("refresh_failed", f"github_{status}")
    return value


def exchange(inputs: Inputs, subject: str, public_key: str) -> str:
    """POST the token exchange; the session token, or RefreshError naming the HTTP status."""
    basic = base64.b64encode(f"{inputs.client_id}:{inputs.client_secret}".encode()).decode()
    form = urllib.parse.urlencode((
        *EXCHANGE_FIELDS, ("subject_token", subject), ("subject_token_type", "jwt"), ("public_key", public_key),
    )).encode()
    headers = {
        "Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    status, body = _call("POST", f"{inputs.domain_url}/oauth2/v1/token", headers, form, "")
    value = _json_field(body, "token") if status == 200 else None
    if value is None:
        raise RefreshError("refresh_failed", str(status))
    return value


def _write_atomically(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_token_atomically(session_dir: Path, value: str) -> None:
    """Replace only the token file; the SDK re-reads it per call and caches the key by path."""
    _write_atomically(session_dir / TOKEN_FILE, value)


def append_log(session_dir: Path, line: str) -> None:
    if not LOG_LINE.fullmatch(line):
        line = "refresh_failed status=error"
    with contextlib.suppress(OSError):
        fd = os.open(session_dir / LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(f"{line}\n")


def refresh_once(inputs: Inputs, announce=None) -> str:
    """One exchange and swap; returns the closed log line it appended."""
    try:
        subject = github_token(inputs)
        value = exchange(inputs, subject, public_key_body(inputs.session_dir / PUBLIC_KEY_FILE))
        if announce is not None:
            announce(value)
        write_token_atomically(inputs.session_dir, value)
        line = "refreshed"
    except RefreshError as err:
        line = f"refresh_failed status={err.code}"
    except OSError:
        line = "refresh_failed status=error"
    append_log(inputs.session_dir, line)
    return line


def cmd_once(env: dict[str, str]) -> int:
    try:
        inputs = validate(env)
    except RefreshError as err:
        _error(err.code)
        return 1
    line = refresh_once(inputs, announce=_mask)
    if line != "refreshed":
        _error(line)
        return 1
    _emit(line)
    return 0


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def owns_pid_file(session_dir: Path) -> bool:
    return _read_pid(session_dir / PID_FILE) == os.getpid()


def cmd_start(env: dict[str, str]) -> int:
    try:
        inputs = validate(env)
    except RefreshError as err:
        _error(err.code)
        return 1
    pid_file = inputs.session_dir / PID_FILE
    existing = _read_pid(pid_file)
    if existing is not None and _pid_alive(existing):
        _emit("already_running")
        return 0
    # The secret reaches the child through its inherited environment, never argv.
    child = _spawn(
        [sys.executable or "python3", str(Path(__file__).resolve()), "loop"],
        env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )
    _write_atomically(pid_file, f"{child.pid}\n")
    _emit(f"started interval_seconds={inputs.interval}")
    return 0


def loop(env: dict[str, str], sleep=None, alive=None) -> int:
    """Refresh every interval, retry a failure after a minute, stop when the pid file goes."""
    try:
        inputs = validate(env)
    except RefreshError:
        return 1
    sleep = _sleep if sleep is None else sleep
    alive = (lambda: owns_pid_file(inputs.session_dir)) if alive is None else alive
    started = _monotonic()
    delay = inputs.interval
    while True:
        sleep(delay)
        if _monotonic() - started >= MAX_LOOP_SECONDS or not alive():
            return 0
        try:
            line = refresh_once(inputs)
        except Exception:  # noqa: BLE001 - one bad round must not end the loop
            line = "refresh_failed status=error"
            append_log(inputs.session_dir, line)
        delay = inputs.interval if line == "refreshed" else RETRY_SECONDS


def report(env: dict[str, str]) -> int:
    """Print the closed lines of the log and one summary line; always 0."""
    refreshed = failed = 0
    session = env.get("OCI_SESSION_DIR", "").strip()
    text = ""
    if session:
        with contextlib.suppress(OSError, ValueError):
            text = (Path(session) / LOG_FILE).read_text(errors="replace")
    for line in text.splitlines():
        if not LOG_LINE.fullmatch(line):
            continue
        _emit(line)
        if line == "refreshed":
            refreshed += 1
        else:
            failed += 1
    _emit(f"refreshed={refreshed} failed={failed}")
    return 0


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("once", "start", "report"):
        sub.add_parser(name)
    sub.add_parser("loop", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    values = dict(os.environ if env is None else env)
    if args.command == "once":
        return cmd_once(values)
    if args.command == "start":
        return cmd_start(values)
    if args.command == "loop":
        return loop(values)
    return report(values)


if __name__ == "__main__":
    sys.exit(main())
