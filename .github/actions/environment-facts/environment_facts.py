#!/usr/bin/env python3
"""Read a scaffolded environment's var-file and decide how to stand it up.

The var-file arrives as JSON — `yq -o json` does the YAML, this does the
decisions, and the tests feed it JSON directly. → see docs/validate-oke.md

    yq -o json '.' terragrunt/demo/eu-frankfurt-1/oke/.boilerplate.yml > vars.json
    python3 .github/actions/environment-facts/environment_facts.py facts --var-file vars.json \\
        --environment-path terragrunt/demo/eu-frankfurt-1/oke
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REVISION = re.compile(r'^(?P<lead>\s*project_repo_revision\s*=\s*)"(?P<value>[^"]*)"\s*$', re.MULTILINE)

SOURCE_ITEM = re.compile(r'^(?P<indent> *)- +repoURL:')
VALUES_REF = re.compile(r'^\s*ref: +values\s*$')
# `[ \t]*$` and not `\s*$`: the line keeps its newline, and a greedy \s would eat it.
TARGET_REVISION = re.compile(r'^(?P<lead>[ \t]*targetRevision: +)"(?P<value>[^"]*)"[ \t]*$')


CLOUDS = ("oci", "aws", "ecs")


def _flag(variables: dict, key: str, absent: bool) -> bool:
    """A var-file's boolean, or what the templates default it to when unset."""
    return bool(variables[key]) if key in variables else absent


def facts(variables: dict, environment_path: str, cloud: str = "oci") -> dict[str, str]:
    """What the workflow needs that is not in its own inputs."""
    if cloud not in CLOUDS:
        raise SystemExit(f"unknown cloud {cloud!r}; expected one of {', '.join(CLOUDS)}")

    missing = [key for key in ("region", "clusterName") if not variables.get(key)]
    if missing:
        raise SystemExit(f"the var-file records no {' and no '.join(missing)}")

    resolved = {
        "region": variables["region"],
        "cluster_name": variables["clusterName"],
        # Names the secrets and resources this environment owns, so a run can
        # scope a cleanup to its own. → docs/validate-eks.md
        "environment": variables.get("environment", ""),
        # The environment root, not the region: `global/` units sit beside the
        # region, and `run --all` only queues what it can see. → docs/validate-eks.md
        "apply_path": str(Path(environment_path).parent.parent),
        # The zone the environment publishes into, so a run can find the names
        # a client would use rather than the load balancer's own.
        "dns_zone": variables.get("domainZone", ""),
    }
    resolved.update({"oci": _oci, "aws": _aws, "ecs": _ecs}[cloud](variables))
    return resolved


def _oci(variables: dict) -> dict[str, str]:
    public = _flag(variables, "controlPlaneIsPublic", absent=False)
    return {
        # The identity domain serving the token exchange is home-region-only.
        "home_region": variables.get("homeRegion") or variables["region"],
        "kube_endpoint": "PUBLIC_ENDPOINT" if public else "PRIVATE_ENDPOINT",
        # A private endpoint is only reachable from inside the tailnet.
        "needs_tailnet": "false" if public else "true",
        "tailscale_enabled": "true" if _flag(variables, "tailscaleEnabled", absent=False) else "false",
        # Matching kubectl to the cluster, rather than to whatever the runner
        # image happens to carry.
        "kubernetes_version": variables.get("kubernetesVersion", ""),
        # Where ESO reads its secrets from, so a failed run can ask OCI which
        # half of its ambiguous 404 it meant.
        "vault_ocid": variables.get("vaultOcid", ""),
        "vault_compartment_ocid": variables.get("vaultCompartmentOcid", ""),
        "compartment_name": variables.get("compartmentName", ""),
        # Where the teardown looks for what it failed to remove; empty when the
        # environment creates its own. → docs/validate-oke.md
        "compartment_ocid": variables.get("compartmentOcid", ""),
        "parent_compartment_ocid": variables.get("parentCompartmentOcid", ""),
    }


def _aws(variables: dict) -> dict[str, str]:
    """The EKS var-file records less than the OKE one. → docs/validate-eks.md"""
    # Absent is not false here: the cluster module defaults to a private-only
    # API server and the stack defaults the subnet router on. → NR-942
    public = _flag(variables, "controlPlaneIsPublic", absent=False)
    return {
        # No home region on AWS; STS is reached in the environment's own.
        "home_region": variables["region"],
        # EKS has no endpoint selector — one DNS name serves both.
        "kube_endpoint": "",
        "needs_tailnet": "false" if public else "true",
        "tailscale_enabled": "true" if _flag(variables, "tailscaleEnabled", absent=True) else "false",
        # Read from the cluster after the apply, not pinned here.
        "kubernetes_version": "",
        "vault_ocid": "",
        "vault_compartment_ocid": "",
        "compartment_name": "",
        "compartment_ocid": "",
        "parent_compartment_ocid": "",
    }


def _ecs(variables: dict) -> dict[str, str]:
    """ECS has no control plane of its own and no subnet router. → docs/validate-ecs.md"""
    return {
        "home_region": variables["region"],
        "kube_endpoint": "",
        # The ECS API is public and the stack's load balancer is internet-facing,
        # so nothing a run touches sits behind a tailnet.
        "needs_tailnet": "false",
        "tailscale_enabled": "false",
        "kubernetes_version": "",
        "vault_ocid": "",
        "vault_compartment_ocid": "",
        "compartment_name": "",
        "compartment_ocid": "",
        "parent_compartment_ocid": "",
    }


def summary(variables: dict, facts_: dict[str, str], environment_path: str, revision: str) -> str:
    lines = [
        f"### Validating `{environment_path}`",
        "",
        f"- cluster: `{facts_['cluster_name']}` in `{facts_['region']}`",
        # Off needs_tailnet, not kube_endpoint: only OKE has the latter, and
        # ECS has no control plane to describe at all.
        f"- network: {'private, reached over the tailnet' if facts_['needs_tailnet'] == 'true' else 'public'}",
    ]
    if revision:
        lines.append(f"- GitOps revision under test: `{revision}`")
    lines += [
        f"- catalog ref the tree was rendered from: `{variables.get('renderedFromRef') or 'unrecorded'}`",
        "",
        "| | version |",
        "|---|---|",
    ]
    for group in ("modulesVersions", "chartVersions"):
        for name, version in sorted((variables.get(group) or {}).items()):
            lines.append(f"| `{name}` | {version} |")
    return "\n".join(lines) + "\n"


def set_revision(text: str, revision: str) -> str:
    """Point the GitOps source at the branch under test, in the checkout only."""
    found = REVISION.findall(text)
    if len(found) != 1:
        raise SystemExit(
            f"expected exactly one project_repo_revision, found {len(found)} — "
            "the environment is not shaped the way the templates emit it"
        )
    return REVISION.sub(lambda match: f'{match.group("lead")}"{revision}"', text)


def set_gitops_revision(text: str, revision: str) -> str:
    """Repoint the ApplicationSet's own copy of the same decision.

    The rendered manifest declares two sources and Argo CD resolves both at sync
    time, so the stack value alone leaves a branch run reading its day-2 values
    from `main`. → see docs/validate-eks.md
    """
    lines = text.splitlines(keepends=True)
    rewritten = 0
    for start, line in enumerate(lines):
        marker = SOURCE_ITEM.match(line)
        if not marker:
            continue
        # The item runs until something no deeper than its own dash, which is
        # either the next source or the end of the sources list.
        depth = len(marker.group("indent"))
        end = start + 1
        while end < len(lines):
            body = lines[end]
            if body.strip() and (len(body) - len(body.lstrip(" "))) <= depth:
                break
            end += 1
        item = lines[start:end]
        # `ref: values` is what makes a source the project repository: it is the
        # one the chart's valueFiles resolve `$values` against.
        if not any(VALUES_REF.match(one) for one in item):
            continue
        for offset, one in enumerate(item):
            replaced, count = TARGET_REVISION.subn(
                lambda match: f'{match.group("lead")}"{revision}"', one
            )
            if count:
                lines[start + offset] = replaced
                rewritten += count

    if rewritten != 1:
        raise SystemExit(
            f"expected exactly one project-repository targetRevision, found {rewritten} — "
            "the manifest is not shaped the way the templates render it"
        )
    return "".join(lines)


def _emit(pairs: dict[str, str]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    handle = open(output, "a", encoding="utf-8") if output else sys.stdout
    try:
        for key, value in pairs.items():
            print(f"{key}={value}", file=handle)
    finally:
        if output:
            handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    fact = commands.add_parser("facts", help="emit the facts as step outputs")
    fact.add_argument("--var-file", required=True, help="the var-file as JSON")
    fact.add_argument("--environment-path", required=True)
    fact.add_argument("--cloud", default="oci", choices=CLOUDS, help="which cloud's var-file shape this is")
    fact.add_argument("--revision", default="", help="the GitOps revision under test")
    fact.add_argument("--summary", action="store_true", help="also write the job summary")

    revise = commands.add_parser("set-revision", help="rewrite project_repo_revision in place")
    revise.add_argument("--stack-file", required=True)
    revise.add_argument("--revision", required=True)

    argo = commands.add_parser(
        "set-gitops-revision", help="rewrite the ApplicationSet's project-repository targetRevision in place"
    )
    argo.add_argument("--manifest", required=True, help="argo/<cluster>/cluster-services.yaml")
    argo.add_argument("--revision", required=True)

    args = parser.parse_args(argv)

    if args.command == "facts":
        variables = json.loads(Path(args.var_file).read_text())
        resolved = facts(variables, args.environment_path, args.cloud)
        _emit(resolved)
        if args.summary:
            text = summary(variables, resolved, args.environment_path, args.revision)
            target = os.environ.get("GITHUB_STEP_SUMMARY")
            if target:
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write(text)
            else:
                print(text, end="")
        return 0

    if args.command == "set-gitops-revision":
        manifest = Path(args.manifest)
        manifest.write_text(set_gitops_revision(manifest.read_text(), args.revision))
        print(f"the project-repository source now tracks {args.revision} in {manifest}")
        return 0

    path = Path(args.stack_file)
    path.write_text(set_revision(path.read_text(), args.revision))
    print(f"project_repo_revision is now {args.revision} in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
