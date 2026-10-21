#!/usr/bin/env python3
"""Post a run's NDJSON events to the Runestra Console as this job.

`EventSink` is the library `apply_units.py` imports; `post --file` wraps lines of
`{"type": ..., "payload": {...}}` in the full envelope for Console-authored bash steps.
Telemetry never fails the job, no event is ever derived from log text here, and known secrets are masked.
Stdlib only. See docs/console-integration.md.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Mirrors COARSE_CHANNEL_TYPES in the Console's api/src/console/domain/runs/events.py.
COARSE_CHANNEL_TYPES = frozenset({
    "run.status_polled",
    "run.reconcile_gave_up",
    "run.gate.deadline_exceeded",
})

LINE_CAP_BYTES = 16 * 1024
TOKEN_MAX_AGE = 240.0
NOT_FOUND_WINDOW = 180.0
RETRY_ATTEMPTS = 5
BACKOFF_START = 1.0
BACKOFF_CAP = 15.0
REQUEST_TIMEOUT = 30.0
TRUNCATION_MARKER = "...[truncated]"
FINAL_STATUSES = (401, 403, 413, 422)
AUTH_STATUSES = (401, 403)
LOG_LINE = "log.line"
RETRY_PAUSE = 30.0
CLOSE_ROUNDS = 2
BUFFER_CAP_EVENTS = 5000
BUFFER_CAP_BYTES = 8 * 1024 * 1024
RUN_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

_sleep = time.sleep
_monotonic = time.monotonic


MASK = "***"
MIN_SECRET_LENGTH = 8
SECRET_ENV_NAMES = frozenset({
    "RUNESTRA_CLIENT_SECRET", "RUNESTRA_REGISTRY_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_RUNTIME_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
})
SECRET_NAME_RE = re.compile(r"SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|API_KEY|ACCESS_KEY|CREDENTIAL", re.I)
ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class SecretMasker:
    """Replaces every secret value this process knows with `***`, longest first.

    Sources: secret-looking environment variables, the lines of CONSOLE_MASK_ENV (`KEY=value`),
    the brokered catalog token in its state file (re-read when it changes), and anything `add`ed.
    """

    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = dict(os.environ if env is None else env)
        self._values: set[str] = set()
        self._state_path = Path(env["RUNNER_TEMP"]) / "catalog-token" / "state.json" if env.get("RUNNER_TEMP") else None
        self._state_stamp: tuple[int, int] | None = None
        self._state_token = ""
        self._ordered: list[str] = []
        for name, value in env.items():
            if name in SECRET_ENV_NAMES or (SECRET_NAME_RE.search(name) and not name.upper().endswith("_URL")):
                self.add(value)
        for line in env.get("CONSOLE_MASK_ENV", "").splitlines():
            found = ENV_LINE_RE.match(line)
            self.add(found.group(2) if found else line)

    def add(self, value: object) -> None:
        """Remember a secret; values under 8 characters are ignored, a multi-line value also masks each line."""
        if not isinstance(value, str):
            return
        for piece in {value, *(line.strip() for line in value.splitlines())}:
            if len(piece) >= MIN_SECRET_LENGTH and piece not in self._values:
                self._values.add(piece)
                self._ordered = sorted(self._values, key=len, reverse=True)

    def _refresh_state(self) -> None:
        if self._state_path is None:
            return
        try:
            stat = self._state_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self._state_stamp:
                return
            token = json.loads(self._state_path.read_text()).get("token", "")
        except (OSError, ValueError, AttributeError):
            return
        self._state_stamp = stamp
        self.add(token)

    def mask(self, text: str) -> str:
        self._refresh_state()
        for value in self._ordered:
            if value in text:
                text = text.replace(value, MASK)
        return text

    def mask_payload(self, value: object) -> object:
        """Every string anywhere in a payload, masked."""
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, dict):
            return {key: self.mask_payload(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.mask_payload(item) for item in value]
        return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102 - a bearer must not follow a redirect
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(req: urllib.request.Request) -> tuple[int, bytes]:
    try:
        with _OPENER.open(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, resp.read(1 << 20)
    except urllib.error.HTTPError as err:
        try:
            return err.code, err.read(1 << 20)
        finally:
            err.close()


def _warning(message: str) -> None:
    print(f"::warning title=console-events::{message}", file=sys.stderr, flush=True)


def _mint_id_token(audience: str) -> str:
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not bearer:
        raise RuntimeError("id_token_unavailable")
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}audience={urllib.parse.quote(audience, safe='')}",
        headers={"Authorization": f"bearer {bearer}", "Accept": "application/json"},
    )
    status, body = _request(req)
    if status != 200:
        raise RuntimeError("id_token_refused")
    value = json.loads(body).get("value")
    if not isinstance(value, str) or not value:
        raise RuntimeError("id_token_malformed")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _serialise(envelope: dict) -> bytes:
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def fit_line(envelope: dict) -> bytes | None:
    """The envelope as one line under the per-line cap, truncating payload.text; None if it cannot fit."""
    raw = _serialise(envelope)
    if len(raw) <= LINE_CAP_BYTES:
        return raw
    text = envelope.get("payload", {}).get("text")
    if not isinstance(text, str):
        return None
    for _ in range(8):
        overflow = len(raw) - LINE_CAP_BYTES
        cut = text.encode("utf-8")
        keep = max(len(cut) - overflow - len(TRUNCATION_MARKER) - 4, 0)
        text = cut[:keep].decode("utf-8", errors="ignore") + TRUNCATION_MARKER
        envelope = {**envelope, "payload": {**envelope["payload"], "text": text}}
        raw = _serialise(envelope)
        if len(raw) <= LINE_CAP_BYTES:
            return raw
    return None


def _body(entries: list[tuple[str, bytes]]) -> bytes:
    return b"\n".join(line for _, line in entries) + b"\n"


def _plural(count: int) -> str:
    return f"{count} event" if count == 1 else f"{count} events"


class EventSink:
    """Buffers envelopes and posts them in batches; every method is safe to call blind.

    A batch the Console could not take goes back to the head of the buffer; when the buffer
    is full, `log.line` events are shed oldest first and structured events are always kept.
    """

    def __init__(self, console_url: str, audience: str, run_id: str, *, batch_size: int = 50,
                 flush_seconds: float = 5.0, masker: SecretMasker | None = None) -> None:
        self.masker = masker if masker is not None else SecretMasker()
        self.enabled = bool(console_url and audience and run_id)
        self.counts = {"sent": 0, "refused": 0, "dropped": 0}
        self._buffer: list[tuple[str, bytes]] = []
        self._buffered_since = 0.0
        self._retry_at = 0.0
        self._shed_count = 0
        self._failure = ""
        self._status = 0
        self._batch_size = max(batch_size, 1)
        self._flush_seconds = flush_seconds
        self._audience = audience
        self._run_id = run_id
        self._warned: set[str] = set()
        self._token = ""
        self._token_at = 0.0
        self._not_found_since: float | None = None
        try:
            self._attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "") or 1)
        except ValueError:
            self._attempt = 1
        self._endpoint = ""
        if self.enabled:
            parsed = urllib.parse.urlsplit(console_url)
            loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
            secure = parsed.scheme == "https" or (parsed.scheme == "http" and loopback)
            if not secure or not parsed.netloc or not RUN_ID_RE.match(run_id):
                self.enabled = False
                self._warn_once("config", "console_url or console_run_id is not usable; events disabled")
            else:
                self._endpoint = f"{console_url.rstrip('/')}/api/v1/runs/{run_id}/events"

    def emit(self, event_type: str, payload: dict | None = None) -> bool:
        """Queue one event. False when it was not queued; never raises."""
        try:
            if not self.enabled:
                return False
            if not isinstance(event_type, str) or not event_type:
                self.counts["dropped"] += 1
                return False
            if event_type in COARSE_CHANNEL_TYPES:
                self.counts["refused"] += 1
                self._warn_once(f"coarse:{event_type}", f"refusing coarse-channel type {event_type}")
                return False
            line = fit_line({
                "run_id": self._run_id, "attempt": self._attempt, "emitted_at": _utc_now(),
                "type": event_type,
                "payload": self.masker.mask_payload(payload) if isinstance(payload, dict) else {},
            })
            if line is None:
                self._lose([(event_type, b"")], "dropped", "it is over the line cap")
                return False
            if not self._buffer:
                self._buffered_since = _monotonic()
            self._buffer.append((event_type, line))
            self._shed()
            due = len(self._buffer) >= self._batch_size or _monotonic() - self._buffered_since >= self._flush_seconds
            if due and _monotonic() >= self._retry_at:
                self.flush()
            return True
        except Exception:  # telemetry must not fail an apply
            self.counts["dropped"] += 1
            return False

    def mask(self, text: str) -> str:
        """The text with every known secret replaced; for callers that cut a line before emitting it."""
        return self.masker.mask(text)

    def flush(self) -> None:
        """Post the buffer in batches until it is empty or the Console stops taking them."""
        try:
            while self._buffer:
                batch = self._buffer[:self._batch_size]
                del self._buffer[:len(batch)]
                if not self._deliver(batch):
                    break
            self._report_shed()
        except Exception:  # telemetry must not fail an apply
            pass

    def close(self) -> None:
        """Flush everything left, ignoring the pause; what the Console still refuses is dropped and named."""
        try:
            for _ in range(CLOSE_ROUNDS):
                if not self._buffer:
                    break
                self._retry_at = 0.0
                self.flush()
            if self._buffer:
                left, self._buffer = self._buffer, []
                self._lose(left, "dropped", f"the job ended before the Console took them ({self._status_text()})")
            self._report_shed()
        except Exception:
            pass

    def summary(self) -> str:
        return f"sent={self.counts['sent']} refused={self.counts['refused']} dropped={self.counts['dropped']}"

    def _deliver(self, batch: list[tuple[str, bytes]]) -> bool:
        """False after a retryable failure, with the batch back at the head of the buffer."""
        if self._try_post(batch):
            self.counts["sent"] += len(batch)
            return True
        if self._failure == "retry":
            self._hold(batch)
            return False
        if self._failure != "refused" or len(batch) == 1 or self._status in AUTH_STATUSES:
            self._lose(batch, *self._loss())
            return True
        structured = [entry for entry in batch if entry[0] != LOG_LINE]
        self._lose([entry for entry in batch if entry[0] == LOG_LINE], *self._loss())
        for index, entry in enumerate(structured):
            if self._try_post([entry]):
                self.counts["sent"] += 1
            elif self._failure == "retry":
                self._hold(structured[index:])
                return False
            else:
                self._lose([entry], *self._loss())
        return True

    def _try_post(self, batch: list[tuple[str, bytes]]) -> bool:
        try:
            return self._post(_body(batch))
        except Exception:
            self._failure, self._status = "error", 0
            return False

    def _loss(self) -> tuple[str, str]:
        if self._failure == "refused":
            return "refused", f"the Console refused them with HTTP {self._status}"
        if self._failure == "unbound":
            return "dropped", "the run is not bound on the Console"
        return "dropped", "posting failed unexpectedly"

    def _status_text(self) -> str:
        return f"last HTTP {self._status}" if self._status else "no response"

    def _hold(self, entries: list[tuple[str, bytes]]) -> None:
        self._buffer[:0] = entries
        self._retry_at = _monotonic() + RETRY_PAUSE
        print(f"console-events: the Console did not accept a batch ({self._status_text()}); "
              f"{_plural(len(self._buffer))} held, next try in {RETRY_PAUSE:.0f} s", file=sys.stderr, flush=True)
        self._shed()

    def _shed(self) -> None:
        size = sum(len(line) for _, line in self._buffer)
        while len(self._buffer) > BUFFER_CAP_EVENTS or size > BUFFER_CAP_BYTES:
            index = next((i for i, (kind, _) in enumerate(self._buffer) if kind == LOG_LINE), None)
            if index is None:
                return
            size -= len(self._buffer.pop(index)[1])
            self._shed_count += 1
            self.counts["dropped"] += 1

    def _report_shed(self) -> None:
        if self._shed_count:
            count, self._shed_count = self._shed_count, 0
            _warning(f"{_plural(count)} dropped ({LOG_LINE} x{count}): the buffer was full while the Console "
                     "did not accept batches; log lines go first")

    def _lose(self, entries: list[tuple[str, bytes]], how: str, reason: str) -> None:
        if not entries:
            return
        self.counts[how] += len(entries)
        kinds = ", ".join(f"{kind} x{count}" for kind, count in Counter(kind for kind, _ in entries).items())
        _warning(f"{_plural(len(entries))} {how} ({kinds}): {reason}")

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            _warning(message)

    def _bearer(self) -> str:
        if not self._token or _monotonic() - self._token_at >= TOKEN_MAX_AGE:
            self._token = _mint_id_token(self._audience)
            self._token_at = _monotonic()
            self.masker.add(self._token)
        return self._token

    def _post(self, body: bytes) -> bool:
        delay, failures = BACKOFF_START, 0
        while True:
            try:
                req = urllib.request.Request(self._endpoint, data=body, method="POST", headers={
                    "Authorization": f"Bearer {self._bearer()}", "Content-Type": "application/x-ndjson"})
                status, _ = _request(req)
            except (urllib.error.URLError, OSError, http.client.HTTPException, RuntimeError, ValueError):
                status = 0
            self._status = status
            if 200 <= status < 300:
                self._not_found_since = None
                return True
            if status in FINAL_STATUSES or (400 <= status < 500 and status not in (404, 408, 429)):
                self._failure = "refused"
                return False
            if status == 404:
                now = _monotonic()
                if self._not_found_since is None:
                    self._not_found_since = now
                if now - self._not_found_since + delay > NOT_FOUND_WINDOW:
                    self._failure = "unbound"
                    return False
            else:
                failures += 1
                if failures >= RETRY_ATTEMPTS:
                    self._failure = "retry"
                    return False
            _sleep(delay)
            delay = min(delay * 2, BACKOFF_CAP)


def cmd_post(path: str, env: dict[str, str]) -> int:
    sink = EventSink(env.get("CONSOLE_URL", "").strip(), env.get("CONSOLE_AUDIENCE", "").strip(),
                     env.get("CONSOLE_RUN_ID", "").strip(), masker=SecretMasker(env))
    if not sink.enabled:
        print("::notice title=console-events::console_url is empty or unusable; nothing to post", flush=True)
        return 0
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        _warning("the events file could not be read")
        return 0
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
            event_type, payload = doc["type"], doc.get("payload", {})
        except (ValueError, KeyError, TypeError):
            _warning(f"line {number} is not an event and was skipped")
            continue
        sink.emit(event_type, payload)
    sink.close()
    print(f"::notice title=console-events::{sink.summary()}", flush=True)
    return 0


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    post = sub.add_parser("post")
    post.add_argument("--file", required=True)
    args = parser.parse_args(argv)
    try:
        return cmd_post(args.file, dict(os.environ if env is None else env))
    except Exception:  # exit 0 even when delivery fails
        _warning("posting events failed unexpectedly")
        return 0


if __name__ == "__main__":
    sys.exit(main())
