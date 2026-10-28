#!/usr/bin/env python3
"""Tell, before Terragrunt starts, whether the client's tailnet will accept this run and its router.

Checks stop at the first failure: presence, token exchange, a key minted and deleted for the join
tag and for the router's exact tags, then (only when the router token reads the policy) the grant
and the route approval. Every Tailscale answer and transport failure exits 0 with result, code and
policy_checked in GITHUB_OUTPUT; no secret, token or minted key is ever printed.
→ docs/tailscale-preflight.md
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.tailscale.com/api/v2"
TIMEOUT_SECONDS = 20
KEY_EXPIRY_SECONDS = 300
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


class Stop(Exception):
    """A check that ended the run's preflight with a closed code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def base_url() -> str:
    """The constant API base; a test may point it at loopback and nowhere else."""
    override = os.environ.get("TAILSCALE_PREFLIGHT_BASE_URL", "")
    if override and urllib.parse.urlsplit(override).hostname in LOOPBACK_HOSTS:
        return override
    return API_BASE


def request(method: str, path: str, token: str = "", body: object = None, form: dict[str, str] | None = None,
            accept: str = "") -> tuple[int, object]:
    """One call. A 5xx, a 429, a timeout or a transport failure is Stop(api_unreachable)."""
    headers = {}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(base_url() + path, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(call, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except (urllib.error.URLError, OSError, ValueError):
        raise Stop("tailscale_api_unreachable") from None
    if status >= 500 or status == 429:
        raise Stop("tailscale_api_unreachable")
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def mask(value: str) -> None:
    print(f"::add-mask::{value}")


def exchange(client_id: str, secret: str, refused: str) -> str:
    status, body = request("POST", "/oauth/token",
                           form={"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"})
    token = body.get("access_token") if isinstance(body, dict) else None
    if status != 200 or not isinstance(token, str) or not token:
        raise Stop(refused)
    mask(token)
    return token


def mint_and_delete(token: str, tags: list[str], refused: str) -> None:
    """Mint a one-use key for these tags, then delete it whatever happens next."""
    body = {"capabilities": {"devices": {"create": {"reusable": False, "ephemeral": True, "preauthorized": True,
                                                    "tags": tags}}}, "expirySeconds": KEY_EXPIRY_SECONDS}
    status, answer = request("POST", "/tailnet/-/keys", token=token, body=body)
    if status >= 400:  # the spike saw 400, 403 and 404; any refusal reads the same
        raise Stop(refused, "requested tags: " + " ".join(tags))
    if isinstance(answer, dict):
        if isinstance(answer.get("key"), str):
            mask(answer["key"])
        key_id = answer.get("id")
        if isinstance(key_id, str) and key_id:
            try:
                request("DELETE", f"/tailnet/-/keys/{urllib.parse.quote(key_id, safe='')}", token=token)
            except Stop:
                pass


def first_host(cidr: str) -> str:
    network = ipaddress.ip_network(cidr, strict=False)
    return str(next(network.hosts(), network.network_address))


def grant_allows(token: str, cidr: str, port: str) -> bool | None:
    """True or False from acl/validate; None when the token cannot read the policy."""
    tests = [{"src": "tag:github", "accept": [f"{first_host(cidr)}:{port}"]}]
    status, body = request("POST", "/tailnet/-/acl/validate", token=token, body=tests)
    if status != 200:
        return None
    return not (isinstance(body, dict) and body.get("data"))


def route_approved(policy: object, cidr: str, tags: list[str]) -> bool:
    routes = (policy.get("autoApprovers") or {}).get("routes") if isinstance(policy, dict) else None
    network = ipaddress.ip_network(cidr, strict=False)
    for key, approvers in (routes or {}).items():
        try:
            covers = network.version == ipaddress.ip_network(key, strict=False).version and network.subnet_of(
                ipaddress.ip_network(key, strict=False))
        except ValueError:
            continue
        if covers and isinstance(approvers, list) and set(approvers) & set(tags):
            return True
    return False


def check_policy(token: str, needs_tailnet: bool, cidr: str, port: str, tags: list[str]) -> bool:
    """Run the optional checks; the return value is policy_checked."""
    status, policy = request("GET", "/tailnet/-/acl", token=token, accept="application/json")
    if status != 200:
        return False
    if needs_tailnet and cidr:
        allowed = grant_allows(token, cidr, port)
        if allowed is None:
            return False
        if not allowed:
            raise Stop("tailscale_grant_missing")
    if cidr and not route_approved(policy, cidr, tags):
        raise Stop("tailscale_route_not_auto_approved")
    return True


def run(env: dict[str, str]) -> tuple[str, str, bool, list[str]]:
    """The ordered checks; returns result, code, policy_checked and the summary lines."""
    needs = env.get("INPUT_NEEDS_TAILNET") == "true"
    router = env.get("INPUT_TAILSCALE_ENABLED") == "true"
    if not (needs or router):
        return "skipped", "", False, ["Neither a join nor a router is needed: nothing to check."]
    join_pair = (env.get("INPUT_JOIN_CLIENT_ID", ""), env.get("INPUT_JOIN_SECRET", ""))
    router_pair = (env.get("INPUT_ROUTER_CLIENT_ID", ""), env.get("INPUT_ROUTER_SECRET", ""))
    tags = env.get("INPUT_ROUTER_TAGS", "").split()
    lines: list[str] = []
    try:
        if (needs and not all(join_pair)) or (router and not all(router_pair)):
            raise Stop("tailscale_credentials_missing")
        lines.append("Credentials present: pass")
        join_token = exchange(*join_pair, "tailscale_join_client_refused") if needs else ""
        router_token = exchange(*router_pair, "tailscale_router_client_refused") if router else ""
        lines.append("Token exchange: pass")
        if needs:
            mint_and_delete(join_token, [env.get("INPUT_JOIN_TAG") or "tag:github"], "tailscale_join_tag_refused")
            lines.append("Join key tag: pass")
        if router:
            mint_and_delete(router_token, tags, "tailscale_router_tags_refused")
            lines.append("Router key tags: pass")
        checked = router and check_policy(router_token, needs, env.get("INPUT_NETWORK_CIDR", ""),
                                          env.get("INPUT_API_PORT") or "443", tags)
        lines.append("Policy checks: " + ("pass" if checked else "not run (the router cannot read the policy)"))
        return "pass", "", bool(checked), lines
    except Stop as stop:
        lines.append(f"Failed: {stop.code}")
        if stop.detail:
            lines.append(stop.detail)
        return "fail", stop.code, False, lines


def write(path: str, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def main() -> int:
    result, code, checked, lines = run(dict(os.environ))
    write(os.environ.get("GITHUB_OUTPUT", ""),
          f"result={result}\ncode={code}\npolicy_checked={'true' if checked else 'false'}\n")
    write(os.environ.get("GITHUB_STEP_SUMMARY", ""), "### Tailnet check\n\n" + "\n".join(f"- {x}" for x in lines) + "\n")
    print(f"Tailnet check: {result}" + (f" ({code})" if code else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
