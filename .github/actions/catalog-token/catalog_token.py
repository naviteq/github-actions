#!/usr/bin/env python3
"""Fetch a brokered catalog read token from the Runestra Console and keep git on it.

`configure` mints this job's Actions OIDC token, asks the Console for a downscoped token
and installs a git credential helper. `credential` is that helper: it answers for the
catalog and terraform-modules repositories only and re-mints when the token is old.
Stdlib only. The token never reaches a job output, an env file or a command line.
See docs/console-integration.md.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BUDGET_SECONDS = 180.0
HELPER_BUDGET_SECONDS = 60.0
BACKOFF_START = 2.0
BACKOFF_CAP = 15.0
REQUEST_TIMEOUT = 30.0
DEFAULT_REFRESH_AFTER = 2700
STATUSES = ("ok", "skipped", "refused", "scope_lost", "unavailable")
SSH_FORMS = ("git@github.com:", "ssh://git@github.com/")
GIT_HTTPS = "https://github.com/"
CRED_SECTION = "credential.https://github.com"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

_sleep = time.sleep
_monotonic = time.monotonic
_wall = time.time


class BrokerError(Exception):
    """A closed `status` and `reason`; the message never carries a token or a body."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102 - a bearer must not follow a redirect
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(req: urllib.request.Request) -> tuple[int, bytes]:
    """(status, body) for any HTTP answer; URLError and OSError for no answer."""
    try:
        with _OPENER.open(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, resp.read(1 << 20)
    except urllib.error.HTTPError as err:
        try:
            return err.code, err.read(1 << 20)
        finally:
            err.close()


_mask_stream = "stdout"  # the credential helper owns stdout, so it masks on stderr


def _mask(value: str) -> None:
    print(f"::add-mask::{value}", file=getattr(sys, _mask_stream), flush=True)


def _error(reason: str) -> None:
    print(f"::error title=catalog-token::{reason}", flush=True)


def _warning(reason: str) -> None:
    print(f"::warning title=catalog-token::{reason}", file=sys.stderr, flush=True)


def validate_console_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise BrokerError("unavailable", "url_not_https")
    if not parsed.netloc or parsed.query or parsed.fragment:
        raise BrokerError("unavailable", "url_invalid")
    return url.rstrip("/")


def mint_id_token(audience: str, env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    url = env.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    bearer = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not bearer:
        raise BrokerError("unavailable", "id_token_unavailable")
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}audience={urllib.parse.quote(audience, safe='')}",
        headers={"Authorization": f"bearer {bearer}", "Accept": "application/json"},
    )
    try:
        status, body = _request(req)
    except (urllib.error.URLError, OSError):
        raise BrokerError("unavailable", "id_token_transport") from None
    if status != 200:
        raise BrokerError("unavailable", "id_token_refused")
    try:
        value = json.loads(body).get("value")
    except (ValueError, AttributeError):
        value = None
    if not isinstance(value, str) or not value:
        raise BrokerError("unavailable", "id_token_malformed")
    return value


def _broker_once(endpoint: str, audience: str) -> tuple[str, str] | None:
    """One broker round trip; None means retry, a BrokerError means stop."""
    id_token = mint_id_token(audience)
    _mask(id_token)
    req = urllib.request.Request(endpoint, data=b"", method="POST", headers={
        "Authorization": f"Bearer {id_token}", "Accept": "application/json"})
    try:
        status, body = _request(req)
    except (urllib.error.URLError, OSError):
        return None
    if status == 200:
        try:
            doc = json.loads(body)
            token, expires = doc.get("token"), doc.get("expires_at")
        except (ValueError, AttributeError):
            raise BrokerError("unavailable", "malformed_response") from None
        if not isinstance(token, str) or not token or not isinstance(expires, str) or not expires:
            raise BrokerError("unavailable", "malformed_response")
        return token, expires
    if status in (401, 403):
        raise BrokerError("refused", "refused")
    if status == 503:
        try:
            code = json.loads(body).get("code")
        except (ValueError, AttributeError):
            code = None
        if code == "catalog_scope_lost":
            raise BrokerError("scope_lost", "scope_lost")
        return None
    if status == 404 or status == 429 or status >= 500:
        return None
    raise BrokerError("unavailable", f"unexpected_http_{status}")


def fetch_token(console_url: str, run_id: str, audience: str, budget: float) -> tuple[str, str]:
    """Token and expires_at from the broker, retrying inside `budget` seconds."""
    if not RUN_ID_RE.match(run_id):
        raise BrokerError("unavailable", "run_id_invalid")
    endpoint = f"{validate_console_url(console_url)}/api/v1/runs/{run_id}/catalog-token"
    deadline = _monotonic() + budget
    delay = BACKOFF_START
    while True:
        got = _broker_once(endpoint, audience)
        if got is not None:
            return got
        if _monotonic() + delay > deadline:
            raise BrokerError("unavailable", "budget_exhausted")
        _sleep(delay)
        delay = min(delay * 2, BACKOFF_CAP)


# State file: 0600 under RUNNER_TEMP, replaced atomically.


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_state(path: Path) -> dict | None:
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) and isinstance(state.get("token"), str) else None


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "config", "--global", *args], capture_output=True, text=True, check=False)


def configure_git(helper_command: str) -> None:
    """Replace, never append: a second rewrite of a prefix would leave the old one winning."""
    listed = _git("--get-regexp", r"^url\..*\.insteadof$").stdout.splitlines()
    for line in listed:
        key, _, value = line.partition(" ")
        if value in SSH_FORMS:
            _git("--unset-all", "--fixed-value", key, value)
    insteadof = f'url.{GIT_HTTPS}.insteadOf'
    _git("--unset-all", insteadof)
    for form in SSH_FORMS:
        _git("--add", insteadof, form)
    _git("--replace-all", f"{CRED_SECTION}.useHttpPath", "true")
    _git("--unset-all", f"{CRED_SECTION}.helper")
    _git("--add", f"{CRED_SECTION}.helper", f"!{helper_command}")


def _allowed(path: str, repositories: list[str]) -> bool:
    norm = path.strip("/").lower().removesuffix(".git")
    return any(norm == repo.lower() or norm.startswith(repo.lower() + "/") for repo in repositories)


def _write_output(status: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target and status in STATUSES:
        with open(target, "a") as handle:
            handle.write(f"status={status}\n")


def _finish(status: str, fail_on_error: bool, reason: str = "") -> int:
    _write_output(status)
    if status in ("ok", "skipped"):
        return 0
    _error(reason or status)
    return 1 if fail_on_error else 0


def cmd_configure(env: dict[str, str]) -> int:
    fail_on_error = env.get("FAIL_ON_ERROR", "true").strip().lower() != "false"
    console_url = env.get("CONSOLE_URL", "").strip()
    if not console_url:
        print("::notice title=catalog-token::console_url is empty; nothing to do", flush=True)
        _write_output("skipped")
        return 0
    audience = env.get("CONSOLE_AUDIENCE", "").strip()
    run_id = env.get("CONSOLE_RUN_ID", "").strip()
    repositories = [env.get("CATALOG_REPOSITORY", "").strip(), env.get("MODULES_REPOSITORY", "").strip()]
    if not audience or not run_id:
        return _finish("unavailable", fail_on_error, "missing_inputs")
    if not all(REPO_RE.match(repo) for repo in repositories):
        return _finish("unavailable", fail_on_error, "repository_invalid")
    try:
        refresh_after = int(env.get("REFRESH_AFTER_SECONDS", "") or DEFAULT_REFRESH_AFTER)
    except ValueError:
        return _finish("unavailable", fail_on_error, "refresh_after_seconds_invalid")
    try:
        token, expires_at = fetch_token(console_url, run_id, audience, BUDGET_SECONDS)
    except BrokerError as err:
        return _finish(err.status, fail_on_error, err.reason)
    _mask(token)
    state_path = Path(env.get("RUNNER_TEMP") or tempfile.gettempdir()) / "catalog-token" / "state.json"
    with _locked(state_path):
        _write_state(state_path, {
            "token": token, "expires_at": expires_at, "minted_at": _wall(),
            "console_url": console_url, "audience": audience, "run_id": run_id,
            "repositories": repositories, "refresh_after_seconds": refresh_after,
        })
    helper = f"{shlex.quote(sys.executable or 'python3')} {shlex.quote(str(Path(__file__).resolve()))}" \
        f" credential --state {shlex.quote(str(state_path))}"
    configure_git(helper)
    return _finish("ok", fail_on_error)


def _refresh_after(state: dict) -> float:
    override = os.environ.get("CATALOG_TOKEN_REFRESH_AFTER_SECONDS", "")
    try:
        return float(override) if override else float(state.get("refresh_after_seconds", DEFAULT_REFRESH_AFTER))
    except (TypeError, ValueError):
        return float(DEFAULT_REFRESH_AFTER)


def current_token(state_path: Path) -> tuple[dict, str] | None:
    """The state and a token no older than the refresh threshold, re-minting under a lock."""
    state = _read_state(state_path)
    if state is None:
        return None
    if _wall() - float(state.get("minted_at", 0)) < _refresh_after(state):
        return state, state["token"]
    with _locked(state_path):
        state = _read_state(state_path) or state
        if _wall() - float(state.get("minted_at", 0)) < _refresh_after(state):
            return state, state["token"]
        try:
            token, expires_at = fetch_token(
                state["console_url"], state["run_id"], state["audience"], HELPER_BUDGET_SECONDS)
        except (BrokerError, KeyError) as err:
            _warning(f"refresh_failed:{getattr(err, 'reason', 'state_incomplete')}")
            return state, state["token"]
        _mask(token)
        state.update(token=token, expires_at=expires_at, minted_at=_wall())
        _write_state(state_path, state)
        return state, token


def cmd_credential(state_path: Path, operation: str, stdin, stdout) -> int:
    global _mask_stream
    if operation != "get":
        return 0
    _mask_stream = "stderr"
    try:
        return _answer(state_path, stdin, stdout)
    finally:
        _mask_stream = "stdout"


def _answer(state_path: Path, stdin, stdout) -> int:
    request: dict[str, str] = {}
    for line in stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, _, value = line.partition("=")
        request[key] = value
    if request.get("host", "").lower() != "github.com" or request.get("protocol", "https") != "https":
        return 0
    state = _read_state(state_path)
    if state is None or not _allowed(request.get("path", ""), state.get("repositories", [])):
        return 0
    got = current_token(state_path)
    if got is None:
        return 0
    stdout.write(f"username=x-access-token\npassword={got[1]}\n")
    stdout.flush()
    return 0


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("configure")
    cred = sub.add_parser("credential")
    cred.add_argument("--state", required=True)
    cred.add_argument("operation", choices=("get", "store", "erase"))
    args = parser.parse_args(argv)
    if args.command == "configure":
        return cmd_configure(dict(os.environ if env is None else env))
    return cmd_credential(Path(args.state), args.operation, sys.stdin, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
