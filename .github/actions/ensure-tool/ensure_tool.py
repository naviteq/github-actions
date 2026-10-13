#!/usr/bin/env python3
"""Keep a tool that is already on PATH at the exact requested version, install it otherwise.

Owns install recipes only, never a version: the caller passes `version` explicitly. Lives
beside its action, not in scripts/, because the public mirror copies actions only.
→ docs/ensure-tool.md

    python3 .github/actions/ensure-tool/ensure_tool.py --tool helm --version 4.3.0 --on-miss install
    python3 .github/actions/ensure-tool/ensure_tool.py --tool helm --probe
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

SEMVER = r"v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)"
EXACT = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
RUNNER_ARCH = {"X64": "amd64", "ARM64": "arm64"}
GH = "https://github.com"
GH_API = "https://api.github.com"


@dataclass(frozen=True)
class Recipe:
    """How to read a tool's version and where its release asset and checksums live.

    `url`, `checksums`, `checksums_order` and `member` are templates over {version} and {arch}.
    `digests` pins SHA-256s by "{version}/{arch}" for a publisher that ships no checksum file.
    """

    probe: tuple[str, ...]
    pattern: str
    url: str = ""
    checksums: str = ""
    checksum_format: str = "sums"
    checksums_order: str = ""
    arch: dict[str, str] = field(default_factory=lambda: {"amd64": "amd64", "arm64": "arm64"})
    binary: str = ""
    installer: str = "download"
    member: str = ""
    digests: dict[str, str] = field(default_factory=dict)


RECIPES: dict[str, Recipe] = {
    "helm": Recipe(
        probe=("helm", "version", "--template", "{{.Version}}"),
        pattern=rf"^{SEMVER}",
        url="https://get.helm.sh/helm-v{version}-linux-{arch}.tar.gz",
        checksums="https://get.helm.sh/helm-v{version}-linux-{arch}.tar.gz.sha256sum",
    ),
    "task": Recipe(
        probe=("task", "--version"),
        pattern=SEMVER,
        url=f"{GH}/go-task/task/releases/download/v{{version}}/task_linux_{{arch}}.tar.gz",
        checksums=f"{GH}/go-task/task/releases/download/v{{version}}/task_checksums.txt",
    ),
    "yq": Recipe(
        probe=("yq", "--version"),
        pattern=rf"version {SEMVER}",
        url=f"{GH}/mikefarah/yq/releases/download/v{{version}}/yq_linux_{{arch}}",
        checksums=f"{GH}/mikefarah/yq/releases/download/v{{version}}/checksums",
        checksum_format="yq",
        checksums_order=f"{GH}/mikefarah/yq/releases/download/v{{version}}/checksums_hashes_order",
    ),
    "conftest": Recipe(
        probe=("conftest", "--version"),
        pattern=rf"Conftest: {SEMVER}",
        url=f"{GH}/open-policy-agent/conftest/releases/download/v{{version}}/conftest_{{version}}_Linux_{{arch}}.tar.gz",
        checksums=f"{GH}/open-policy-agent/conftest/releases/download/v{{version}}/checksums.txt",
        arch={"amd64": "x86_64", "arm64": "arm64"},
    ),
    "terragrunt": Recipe(
        probe=("terragrunt", "--version"),
        pattern=rf"version {SEMVER}",
        url=f"{GH}/gruntwork-io/terragrunt/releases/download/v{{version}}/terragrunt_linux_{{arch}}",
        checksums=f"{GH}/gruntwork-io/terragrunt/releases/download/v{{version}}/SHA256SUMS",
    ),
    "tofu": Recipe(
        probe=("tofu", "version", "-json"),
        pattern=rf'"terraform_version":\s*"{SEMVER}"',
        url=f"{GH}/opentofu/opentofu/releases/download/v{{version}}/tofu_{{version}}_linux_{{arch}}.tar.gz",
        checksums=f"{GH}/opentofu/opentofu/releases/download/v{{version}}/tofu_{{version}}_SHA256SUMS",
    ),
    "terraform": Recipe(
        probe=("terraform", "version", "-json"),
        pattern=rf'"terraform_version":\s*"{SEMVER}"',
        url="https://releases.hashicorp.com/terraform/{version}/terraform_{version}_linux_{arch}.zip",
        checksums="https://releases.hashicorp.com/terraform/{version}/terraform_{version}_SHA256SUMS",
    ),
    "tflint": Recipe(
        probe=("tflint", "--version"),
        pattern=rf"TFLint version {SEMVER}",
        url=f"{GH}/terraform-linters/tflint/releases/download/v{{version}}/tflint_linux_{{arch}}.zip",
        checksums=f"{GH}/terraform-linters/tflint/releases/download/v{{version}}/checksums.txt",
    ),
    "tfsec": Recipe(
        probe=("tfsec", "--version"),
        pattern=SEMVER,
        url=f"{GH}/aquasecurity/tfsec/releases/download/v{{version}}/tfsec-linux-{{arch}}",
        checksums=f"{GH}/aquasecurity/tfsec/releases/download/v{{version}}/tfsec_checksums.txt",
    ),
    "terraform-docs": Recipe(
        probe=("terraform-docs", "--version"),
        pattern=rf"version {SEMVER}",
        url=f"{GH}/terraform-docs/terraform-docs/releases/download/v{{version}}/terraform-docs-v{{version}}-linux-{{arch}}.tar.gz",
        checksums=f"{GH}/terraform-docs/terraform-docs/releases/download/v{{version}}/terraform-docs-v{{version}}.sha256sum",
    ),
    "gitleaks": Recipe(
        probe=("gitleaks", "version"),
        pattern=SEMVER,
        url=f"{GH}/gitleaks/gitleaks/releases/download/v{{version}}/gitleaks_{{version}}_linux_{{arch}}.tar.gz",
        checksums=f"{GH}/gitleaks/gitleaks/releases/download/v{{version}}/gitleaks_{{version}}_checksums.txt",
        arch={"amd64": "x64", "arm64": "arm64"},
    ),
    "kubeconform": Recipe(
        probe=("kubeconform", "-v"),
        pattern=SEMVER,
        url=f"{GH}/yannh/kubeconform/releases/download/v{{version}}/kubeconform-linux-{{arch}}.tar.gz",
        checksums=f"{GH}/yannh/kubeconform/releases/download/v{{version}}/CHECKSUMS",
    ),
    "ct": Recipe(
        probe=("ct", "version"),
        pattern=rf"Version:\s+{SEMVER}",
        url=f"{GH}/helm/chart-testing/releases/download/v{{version}}/chart-testing_{{version}}_linux_{{arch}}.tar.gz",
        checksums=f"{GH}/helm/chart-testing/releases/download/v{{version}}/checksums.txt",
    ),
    "promtool": Recipe(
        probe=("promtool", "--version"),
        pattern=rf"promtool, version {SEMVER}",
        url=f"{GH}/prometheus/prometheus/releases/download/v{{version}}/prometheus-{{version}}.linux-{{arch}}.tar.gz",
        checksums=f"{GH}/prometheus/prometheus/releases/download/v{{version}}/sha256sums.txt",
    ),
    "aws-nuke": Recipe(
        probe=("aws-nuke", "version"),
        pattern=rf"version {SEMVER}",
        url=f"{GH}/ekristen/aws-nuke/releases/download/v{{version}}/aws-nuke-v{{version}}-linux-{{arch}}.tar.gz",
        checksums=f"{GH}/ekristen/aws-nuke/releases/download/v{{version}}/checksums.txt",
    ),
    "kubectl": Recipe(
        probe=("kubectl", "version", "--client", "-o", "json"),
        pattern=rf'"gitVersion":\s*"{SEMVER}"',
        url="https://dl.k8s.io/release/v{version}/bin/linux/{arch}/kubectl",
        checksums="https://dl.k8s.io/release/v{version}/bin/linux/{arch}/kubectl.sha256",
        checksum_format="single",
    ),
    "kind": Recipe(
        probe=("kind", "version"),
        pattern=rf"^kind {SEMVER}",
        url=f"{GH}/kubernetes-sigs/kind/releases/download/v{{version}}/kind-linux-{{arch}}",
        checksums=f"{GH}/kubernetes-sigs/kind/releases/download/v{{version}}/kind-linux-{{arch}}.sha256sum",
    ),
    "pre-commit": Recipe(
        probe=("pre-commit", "--version"),
        pattern=rf"pre-commit {SEMVER}",
        installer="pypi",
    ),
    "age": Recipe(
        probe=("age", "--version"),
        pattern=rf"^{SEMVER}",
        url=f"{GH}/FiloSottile/age/releases/download/v{{version}}/age-v{{version}}-linux-{{arch}}.tar.gz",
        digests={
            "1.3.1/amd64": "bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377",
            "1.3.1/arm64": "c6878a324421b69e3e20b00ba17c04bc5c6dab0030cfe55bf8f68fa8d9e9093a",
            "1.3.2/amd64": "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10",
            "1.3.2/arm64": "6b8dc4333c53a5a57c9e5834e3a48f92605d7154014cd07269ff3327db5d37f4",
        },
    ),
    "infracost": Recipe(
        probe=("infracost", "--version"),
        pattern=rf"Infracost {SEMVER}",
        url=f"{GH}/infracost/infracost/releases/download/v{{version}}/infracost-linux-{{arch}}.tar.gz",
        member="infracost-linux-{arch}",
        digests={
            "0.10.46/amd64": "d0d081cd39b07b2ca5c315830bfc4bcdfb0183b04c19cb18835c154482a2c97b",
            "0.10.46/arm64": "acd693c7d001fef44787bc5de65fffcb25bf5445885fd1da98a7b31281e32b43",
        },
    ),
    "boilerplate": Recipe(
        probe=("boilerplate", "--version"),
        pattern=rf"version {SEMVER}",
        url=f"{GH}/gruntwork-io/boilerplate/releases/download/v{{version}}/boilerplate_linux_{{arch}}",
        checksums=f"{GH_API}/repos/gruntwork-io/boilerplate/releases/tags/v{{version}}",
        checksum_format="github-digest",
    ),
    "shellcheck": Recipe(
        probe=("shellcheck", "--version"),
        pattern=rf"^version: {SEMVER}",
        url=f"{GH}/koalaman/shellcheck/releases/download/v{{version}}/shellcheck-v{{version}}.linux.{{arch}}.tar.gz",
        checksums=f"{GH_API}/repos/koalaman/shellcheck/releases/tags/v{{version}}",
        checksum_format="github-digest",
        arch={"amd64": "x86_64", "arm64": "aarch64"},
    ),
    "oci-cli": Recipe(
        probe=("oci", "--version"),
        pattern=rf"^{SEMVER}",
        binary="oci",
        installer="pypi",
    ),
    "pyflakes": Recipe(
        probe=("pyflakes", "--version"),
        pattern=rf"^{SEMVER}",
        installer="pypi",
    ),
}


class EnsureError(Exception):
    """A failure the job should report as one annotated line."""


def normalize_version(raw: str) -> str:
    """The bare exact version, or an error naming why a range is refused."""
    value = raw.strip()
    if not EXACT.match(value):
        raise EnsureError(
            f"version {raw!r} is not an exact version (X.Y.Z, optional leading v); "
            "ranges, wildcards, partial versions and 'latest' are rejected"
        )
    return value[1:] if value.startswith("v") else value


def asset_arch(recipe: Recipe, runner_arch: str) -> str:
    if runner_arch not in RUNNER_ARCH:
        raise EnsureError(f"unsupported runner architecture {runner_arch!r}; supported: X64, ARM64")
    return recipe.arch[RUNNER_ARCH[runner_arch]]


def render(template: str, version: str, arch: str) -> str:
    return template.format(version=version, arch=arch)


def parse_version(recipe: Recipe, output: str) -> str:
    match = re.search(recipe.pattern, output, re.MULTILINE)
    return match.group(1) if match else ""


def expected_sha256(fmt: str, text: str, asset: str, order: str = "") -> str:
    """The publisher's SHA-256 for asset from a checksum file, or from GitHub's release asset digest."""
    if fmt == "github-digest":
        try:
            assets = json.loads(text).get("assets") or []
        except (ValueError, AttributeError):
            assets = []
        digest = next((str(a.get("digest") or "") for a in assets if a.get("name") == asset), "")
        digest = digest.removeprefix("sha256:")
    elif fmt == "single":
        digest = text.split()[0] if text.split() else ""
    elif fmt == "yq":
        names = [line.strip() for line in order.splitlines() if line.strip()]
        if "SHA-256" not in names:
            raise EnsureError("checksums_hashes_order has no SHA-256 column")
        column = names.index("SHA-256") + 1
        digest = ""
        for line in text.splitlines():
            fields = line.split()
            if fields and fields[0] == asset and len(fields) > column:
                digest = fields[column]
                break
    else:
        digest = ""
        for line in text.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1].lstrip("*").removeprefix("./") == asset:
                digest = fields[0]
                break
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise EnsureError(f"no SHA-256 for {asset} in the publisher's checksum file")
    return digest.lower()


def pinned_sha256(tool: str, recipe: Recipe, version: str, arch: str) -> str:
    """The SHA-256 the recipe pins for this version and architecture; no pin, no install."""
    digest = recipe.digests.get(f"{version}/{arch}", "")
    if not digest:
        pinned = ", ".join(sorted(recipe.digests))
        raise EnsureError(f"{tool} {version} ({arch}) has no pinned SHA-256; pinned: {pinned}")
    return digest.lower()


def bin_dir() -> Path:
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return Path(base) / "bin"


def state_dir() -> Path:
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return Path(base) / "ensure-tool"


def probe(tool: str, recipe: Recipe) -> tuple[str, str]:
    """(resolved version, path) of the tool PATH resolves, empty strings when absent."""
    path = shutil.which(recipe.probe[0])
    if not path:
        return "", ""
    env = dict(os.environ, CHECKPOINT_DISABLE="1", INFRACOST_SKIP_UPDATE_CHECK="true")
    try:
        done = subprocess.run(recipe.probe, capture_output=True, text=True, timeout=120, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{tool}: version probe failed: {exc}", file=sys.stderr)
        return "", path
    return parse_version(recipe, done.stdout + "\n" + done.stderr), path


def curl(url: str, dest: Path) -> None:
    print(f"Downloading {url}", flush=True)
    command = ["curl", "-fsSL", "--retry", "5", "--retry-all-errors", "-o", str(dest), url]
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and url.startswith(f"{GH_API}/"):
        # Fed on stdin so the token never shows in the process list.
        command[1:1] = ["-K", "-"]
        subprocess.run(command, check=True, text=True, input=f'header = "Authorization: Bearer {token}"\n')
        return
    subprocess.run(command, check=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract(archive: Path, name: str, dest: Path) -> None:
    """Copy the regular file called name, at any depth, out of a tarball or zip."""
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if not info.is_dir() and Path(info.filename).name == name:
                    with zf.open(info) as src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out)
                    return
    else:
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if member.isfile() and Path(member.name).name == name:
                    src = tf.extractfile(member)
                    assert src is not None
                    with src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out)
                    return
    raise EnsureError(f"{name} not found in {archive.name}")


def place(src: Path, name: str) -> Path:
    target_dir = bin_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    staged = target_dir / f".{name}.partial"
    shutil.copyfile(src, staged)
    staged.chmod(staged.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH | stat.S_IRUSR)
    target = target_dir / name
    os.replace(staged, target)
    return target


def install_download(tool: str, recipe: Recipe, version: str) -> None:
    if os.environ.get("RUNNER_OS", "Linux") != "Linux":
        raise EnsureError(f"installing is supported on Linux runners only (got {os.environ['RUNNER_OS']})")
    arch = asset_arch(recipe, os.environ.get("RUNNER_ARCH", ""))
    url = render(recipe.url, version, arch)
    asset = url.rsplit("/", 1)[1]
    binary = recipe.binary or tool
    member = render(recipe.member, version, arch) if recipe.member else binary
    expected = pinned_sha256(tool, recipe, version, arch) if recipe.digests else ""
    state_dir().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=state_dir()) as tmp:
        work = Path(tmp)
        curl(url, work / asset)
        if not expected:
            sums = work / "checksums"
            curl(render(recipe.checksums, version, arch), sums)
            order = ""
            if recipe.checksums_order:
                curl(render(recipe.checksums_order, version, arch), work / "order")
                order = (work / "order").read_text()
            expected = expected_sha256(recipe.checksum_format, sums.read_text(), asset, order)
        actual = sha256(work / asset)
        if actual != expected:
            raise EnsureError(f"checksum mismatch for {asset}: expected {expected}, got {actual}")
        print(f"Verified SHA-256 of {asset}", flush=True)
        if asset.endswith((".tar.gz", ".zip")):
            extract(work / asset, member, work / binary)
            src = work / binary
        else:
            src = work / asset
        place(src, binary)
        if tool == "ct":
            install_ct_config(work / asset, version)


def install_ct_config(archive: Path, version: str) -> None:
    """ct reads chart_schema.yaml and lintconf.yaml from CT_CONFIG_DIR, as chart-testing-action sets it."""
    etc = state_dir() / f"ct-{version}" / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    for name in ("chart_schema.yaml", "lintconf.yaml"):
        extract(archive, name, etc / name)
    export("GITHUB_ENV", f"CT_CONFIG_DIR={etc}")
    os.environ["CT_CONFIG_DIR"] = str(etc)


def install_pypi(tool: str, version: str) -> None:
    """uv when the image has it, else a private venv: both stay user-owned and need no sudo."""
    target_dir = bin_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    binary = RECIPES[tool].binary or tool
    spec = f"{tool}=={version}"
    if shutil.which("uv"):
        env = dict(os.environ, UV_TOOL_DIR=str(state_dir() / "uv-tools"), UV_TOOL_BIN_DIR=str(target_dir))
        subprocess.run(["uv", "tool", "install", "--force", "--quiet", spec], check=True, env=env)
        return
    venv = state_dir() / f"{tool}-{version}"
    subprocess.run([sys.executable, "-m", "venv", "--clear", str(venv)], check=True)
    subprocess.run(
        [str(venv / "bin" / "python"), "-m", "pip", "install", "--disable-pip-version-check", "--retries", "5", "--quiet", spec],
        check=True,
    )
    link = target_dir / binary
    link.unlink(missing_ok=True)
    link.symlink_to(venv / "bin" / binary)


def export(variable: str, line: str) -> None:
    target = os.environ.get(variable)
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def summary_line(tool: str, requested: str, resolved: str, source: str) -> str:
    return f"tool={tool} requested={requested} resolved={resolved or 'none'} source={source}"


def ensure(tool: str, raw_version: str, on_miss: str) -> int:
    if tool not in RECIPES:
        raise EnsureError(f"unknown tool {tool!r}; supported: {', '.join(sorted(RECIPES))}")
    if on_miss not in ("install", "fail"):
        raise EnsureError(f"on_miss must be install or fail, got {on_miss!r}")
    recipe = RECIPES[tool]
    version = normalize_version(raw_version)

    found, found_path = probe(tool, recipe)
    print(f"{tool}: requested {version}, found {found or 'nothing'}{f' at {found_path}' if found_path else ''}")
    if found == version:
        source = "baked"
    elif on_miss == "fail":
        line = summary_line(tool, version, found, "missing")
        export("GITHUB_STEP_SUMMARY", f"`{line}`")
        export("GITHUB_OUTPUT", "source=missing")
        export("GITHUB_OUTPUT", f"resolved={found}")
        export("GITHUB_OUTPUT", f"path={found_path}")
        raise EnsureError(f"{tool} {version} is not on PATH and on_miss is fail ({line})")
    else:
        source = "installed"
        if recipe.installer == "pypi":
            install_pypi(tool, version)
        elif recipe.installer == "awscli":
            install_awscli(tool, recipe, version)
        else:
            install_download(tool, recipe, version)
        os.environ["PATH"] = f"{bin_dir()}{os.pathsep}{os.environ.get('PATH', '')}"
        export("GITHUB_PATH", str(bin_dir()))

    resolved, path = probe(tool, recipe)
    line = summary_line(tool, version, resolved, source)
    print(line)
    export("GITHUB_STEP_SUMMARY", f"`{line}`")
    export("GITHUB_OUTPUT", f"source={source}")
    export("GITHUB_OUTPUT", f"resolved={resolved}")
    export("GITHUB_OUTPUT", f"path={path}")
    if resolved != version:
        raise EnsureError(f"{tool} resolves to {resolved or 'nothing'} after {source}, not {version} ({path})")
    return 0


def report(tool: str) -> int:
    """Output the version and path of the tool PATH resolves, installing nothing; absent fails."""
    if tool not in RECIPES:
        raise EnsureError(f"unknown tool {tool!r}; supported: {', '.join(sorted(RECIPES))}")
    found, path = probe(tool, RECIPES[tool])
    if not path:
        raise EnsureError(f"{tool} is not on PATH")
    if not found:
        raise EnsureError(f"{path} reports no {tool} version")
    print(f"{tool}: found {found} at {path}")
    export("GITHUB_OUTPUT", f"resolved={found}")
    export("GITHUB_OUTPUT", f"path={path}")
    return 0


# aws: the zip installer, verified by the publisher's PGP signature (AWS publishes no SHA-256).
# → see docs/ensure-tool.md
AWS_CLI_KEY = Path(__file__).with_name("aws-cli-public-key.asc")
AWS_CLI_KEY_FINGERPRINT = "FB5D B77F D5C1 18B8 0511 ADA8 A631 0ACC 4672 475C".replace(" ", "")

RECIPES["aws"] = Recipe(
    probe=("aws", "--version"),
    pattern=rf"aws-cli/{SEMVER}",
    url="https://awscli.amazonaws.com/awscli-exe-linux-{arch}-{version}.zip",
    arch={"amd64": "x86_64", "arm64": "aarch64"},
    installer="awscli",
)


def verify_signature(archive: Path, signature: Path, key: Path, fingerprint: str, home: Path) -> None:
    """Fail unless gpg reports a valid signature by exactly the key with this fingerprint."""
    gpg = shutil.which("gpg")
    if not gpg:
        raise EnsureError("gpg is not on PATH, so the PGP signature of the download cannot be verified")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = dict(os.environ, GNUPGHOME=str(home))
    imported = subprocess.run([gpg, "--batch", "--quiet", "--import", str(key)], capture_output=True, text=True, env=env)
    if imported.returncode != 0:
        raise EnsureError(f"gpg could not import {key.name}: {imported.stderr.strip()}")
    done = subprocess.run(
        [gpg, "--batch", "--status-fd", "1", "--verify", str(signature), str(archive)],
        capture_output=True, text=True, env=env,
    )
    for line in done.stdout.splitlines():
        fields = line.split()
        if done.returncode == 0 and fields[:2] == ["[GNUPG:]", "VALIDSIG"] and fingerprint in (fields[2], fields[-1]):
            return
    raise EnsureError(f"PGP signature of {archive.name} is not valid for key {fingerprint}: {done.stderr.strip()}")


def install_awscli(tool: str, recipe: Recipe, version: str) -> None:
    if os.environ.get("RUNNER_OS", "Linux") != "Linux":
        raise EnsureError(f"installing is supported on Linux runners only (got {os.environ['RUNNER_OS']})")
    if not shutil.which("unzip"):
        raise EnsureError("unzip is not on PATH, so the AWS CLI archive cannot be unpacked")
    arch = asset_arch(recipe, os.environ.get("RUNNER_ARCH", ""))
    url = render(recipe.url, version, arch)
    asset = url.rsplit("/", 1)[1]
    state_dir().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=state_dir()) as tmp:
        work = Path(tmp)
        curl(url, work / asset)
        curl(f"{url}.sig", work / f"{asset}.sig")
        verify_signature(work / asset, work / f"{asset}.sig", AWS_CLI_KEY, AWS_CLI_KEY_FINGERPRINT, work / "gnupg")
        print(f"Verified PGP signature of {asset} against key {AWS_CLI_KEY_FINGERPRINT}", flush=True)
        subprocess.run(["unzip", "-q", str(work / asset), "-d", str(work / "unpacked")], check=True)
        bin_dir().mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [str(work / "unpacked" / "aws" / "install"), "--install-dir", str(bin_dir().parent / "aws-cli"),
             "--bin-dir", str(bin_dir()), "--update"],
            check=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tool", required=True)
    parser.add_argument("--version")
    parser.add_argument("--on-miss", default="install")
    parser.add_argument("--probe", action="store_true", help="report the version on PATH instead of ensuring one")
    args = parser.parse_args(argv)
    if not args.probe and args.version is None:
        parser.error("--version is required unless --probe is given")
    try:
        if args.probe:
            return report(args.tool.strip())
        return ensure(args.tool.strip(), args.version, args.on_miss.strip())
    except EnsureError as exc:
        print(f"::error title=ensure-tool::{exc}")
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"::error title=ensure-tool::{args.tool}: {' '.join(map(str, exc.cmd))} exited {exc.returncode}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
