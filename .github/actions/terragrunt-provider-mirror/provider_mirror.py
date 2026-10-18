#!/usr/bin/env python3
"""Point the engine and Terragrunt's provider cache at a provider network mirror.

Neither OpenTofu nor Terraform falls back to the registry when a network_mirror errors,
so the mirror is probed first and the CLI config is written only when it answers; when it
does not, nothing is written and providers download directly, with a warning. The config
goes to a file named by TF_CLI_CONFIG_FILE, which both engines and Terragrunt's cache
server read, and carries over the runner's own CLI config. It lives beside its action
because the public mirror copies actions only. → docs/terragrunt-provider-mirror.md

    python3 .github/actions/terragrunt-provider-mirror/provider_mirror.py \\
        --url https://terraform-mirror.example.com/v1/mirror/ --engine tofu \\
        --config-file "$RUNNER_TEMP/terragrunt-provider-mirror.tfrc"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PUBLIC_REGISTRIES = ("registry.terraform.io", "registry.opentofu.org")
ENGINE_REGISTRY = {"terraform": "registry.terraform.io", "tofu": "registry.opentofu.org"}
PROBE_PROVIDER = "hashicorp/null"
PROBE_TIMEOUT = 5
# Quotes, backslashes and template markers would break out of the HCL string.
URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")
PROVIDER_INSTALLATION_RE = re.compile(r"^\s*\"?provider_installation\"?\s*[{:=]", re.MULTILINE)
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


class MirrorError(Exception):
    """The input cannot be used as a network mirror URL."""


def normalize_url(url: str) -> str:
    """The mirror base URL with one trailing slash, or MirrorError for anything else."""
    url = url.strip()
    if not URL_RE.match(url):
        raise MirrorError(f"'{url}' is not an http(s):// URL of the form https://<host>/v1/mirror/")
    return url.rstrip("/") + "/"


def probe_url(base: str, engine: str) -> str:
    return f"{base}{ENGINE_REGISTRY.get(engine, PUBLIC_REGISTRIES[0])}/{PROBE_PROVIDER}/index.json"


def probe(url: str, timeout: float = PROBE_TIMEOUT) -> str | None:
    """None when the mirror lists versions for the probe provider, else why it did not."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read(1 << 20)
    except urllib.error.HTTPError as err:
        return f"HTTP {err.code}"
    except (urllib.error.URLError, OSError) as err:
        return str(getattr(err, "reason", err))
    try:
        versions = json.loads(body).get("versions")
    except (ValueError, AttributeError):
        return "the answer is not a network mirror version list"
    if not isinstance(versions, dict) or not versions:
        return "the answer lists no versions"
    return None


def affected_terraform(version: str) -> bool:
    """Terraform 1.11.0 to 1.12.0 sends HEAD to a mirror's redirect target, which a presigned S3 GET refuses."""
    match = VERSION_RE.match(version.lstrip("v"))
    if not match:
        return False
    triple = tuple(int(part) for part in match.groups())
    return (1, 11, 0) <= triple <= (1, 12, 0)


def terraform_version() -> str:
    try:
        result = subprocess.run(["terraform", "version", "-json"], capture_output=True, text=True,
                                check=False, timeout=30)
        return str(json.loads(result.stdout).get("terraform_version", ""))
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def base_config(engine: str, env: dict[str, str], home: Path) -> Path | None:
    """The CLI config file the engine would read without us, the way Terragrunt resolves it."""
    for name in ("TF_CLI_CONFIG_FILE", "TERRAFORM_CONFIG"):
        if env.get(name):
            path = Path(env[name])
            return path if path.is_file() else None
    candidates = [home / ".terraformrc"]
    if engine != "terraform":
        candidates = [home / ".tofurc", home / ".terraformrc"]
        if env.get("XDG_CONFIG_HOME"):
            candidates.append(Path(env["XDG_CONFIG_HOME"]) / "opentofu" / "tofurc")
    return next((path for path in candidates if path.is_file()), None)


def render(url: str, base: str = "") -> str:
    """base followed by a provider_installation block that mirrors the two public registries."""
    include = ", ".join(f'"{registry}/*/*"' for registry in PUBLIC_REGISTRIES)
    block = (
        "provider_installation {\n"
        "  network_mirror {\n"
        f'    url     = "{url}"\n'
        f"    include = [{include}]\n"
        "  }\n"
        "  direct {}\n"
        "}\n"
    )
    if base and not base.endswith("\n"):
        base += "\n"
    return f"{base}\n{block}" if base else block


def append(path: str | None, line: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def main(argv: list[str] | None = None, env: dict[str, str] | None = None, home: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="")
    parser.add_argument("--engine", default="tofu")
    parser.add_argument("--config-file", required=True)
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env
    home = Path.home() if home is None else home

    def skip(title: str, message: str) -> int:
        print(f"::warning title={title}::{message} Providers download directly from the registries.")
        append(env.get("GITHUB_OUTPUT"), "enabled=false")
        return 0

    if not args.url.strip():
        append(env.get("GITHUB_OUTPUT"), "enabled=false")
        return 0
    try:
        url = normalize_url(args.url)
    except MirrorError as err:
        print(f"::error title=Bad provider_mirror_url::{err}")
        return 1

    if args.engine == "terraform":
        version = terraform_version()
        if affected_terraform(version):
            return skip("Provider mirror skipped", f"Terraform {version} cannot download through a mirror "
                        "that redirects to S3; 1.12.1 fixes it.")

    if url.startswith("http://"):
        print("::notice title=Plain-HTTP provider mirror::Only Terragrunt's provider cache server fetches "
              "through it; terraform and tofu log a non-fatal 'Invalid URL for provider installation source' "
              "on each init and leave installs to the cache server.")

    target = probe_url(url, args.engine)
    reason = probe(target)
    if reason:
        return skip("Provider mirror unreachable", f"{target} did not answer ({reason}).")

    base = base_config(args.engine, env, home)
    base_text = base.read_text(encoding="utf-8") if base else ""
    if base_text.lstrip().startswith("{"):
        return skip("Provider mirror skipped", f"{base} is JSON, which a provider_installation block cannot extend.")
    if PROVIDER_INSTALLATION_RE.search(base_text):
        return skip("Provider mirror skipped", f"{base} already sets provider_installation.")

    out = Path(args.config_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(url, base_text), encoding="utf-8")
    append(env.get("GITHUB_ENV"), f"TF_CLI_CONFIG_FILE={out.resolve()}")
    append(env.get("GITHUB_OUTPUT"), "enabled=true")
    carried = f", carrying over {base}" if base else ""
    print(f"Providers from {', '.join(PUBLIC_REGISTRIES)} come through {url}{carried}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
